"""Crash-safe local outbox and content store. There is deliberately no uploader."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .protocol import CHUNK_SIZE, HEX, canonical, digest, validate_record
from .sqlite_init import initialize


class CapacityError(OSError):
    pass


def native_path(path: Path) -> str:
    """OS-level path string. Windows gets the extended-length prefix so content
    directories (two 64-hex segments under the capture root) work past MAX_PATH."""
    if os.name == "nt":
        text = str(path if path.is_absolute() else Path.cwd() / path)
        if not text.startswith("\\\\?\\"):
            text = "\\\\?\\" + text
        return text
    return str(path)


def _fsync_directory(path: Path) -> None:
    # Directory handles cannot be fsynced on Windows (os.open raises PermissionError).
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    os.makedirs(native_path(path.parent), mode=0o700, exist_ok=True)
    # Short random suffix: the content file name is already 64 hex characters.
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
    temp_native = native_path(temp)
    try:
        fd = os.open(temp_native, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_native, native_path(path))
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temp_native)
        except FileNotFoundError:
            pass


class TraceJournal:
    def __init__(self, root: str | Path, destination: str, *, quota: int = 10 * 1024**3):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            self.root / "journal.sqlite3", timeout=10, isolation_level=None, check_same_thread=False
        )
        os.chmod(self.root / "journal.sqlite3", 0o600)
        self.db.row_factory = sqlite3.Row
        initialize(self.db, self.root / "journal.sqlite3", """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records(
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL, run_id TEXT, body BLOB NOT NULL, created REAL NOT NULL,
                batch_id TEXT, acknowledged INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS record_queue ON records(acknowledged,batch_id,ordinal);
            CREATE INDEX IF NOT EXISTS record_run ON records(run_id,type);
            CREATE TABLE IF NOT EXISTS batches(
                id TEXT PRIMARY KEY, envelope BLOB NOT NULL, state TEXT NOT NULL,
                lease_until REAL NOT NULL, created REAL NOT NULL);
        """)
        self.quota = quota
        self.epoch = uuid.uuid4().hex
        self.producer_id = uuid.uuid4().hex
        self.seq = 0
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('destination',?)", (destination,))
            if (
                self.db.execute("SELECT value FROM meta WHERE key='destination'").fetchone()[0]
                != destination
            ):
                raise ValueError("journal_destination_mismatch")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('source_id',?)", (uuid.uuid4().hex,))
            self.source_id = self.db.execute(
                "SELECT value FROM meta WHERE key='source_id'"
            ).fetchone()[0]
        self.destination = destination
        self.content = ContentStore(self)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _capacity(self, addition: int) -> None:
        # Bound both durable queue and on-disk content, including already ACKed retention.
        size = 0
        for directory, _, names in os.walk(native_path(self.root)):
            for name in names:
                try:
                    size += os.stat(os.path.join(directory, name)).st_size
                except FileNotFoundError:
                    continue  # temporary file replaced concurrently
        if size + addition > self.quota:
            raise CapacityError("trace_storage_capacity")

    def _insert(self, record: dict, run_id: str | None = None) -> None:
        validate_record(record)
        body = canonical(record)
        old = self.db.execute("SELECT body FROM records WHERE id=?", (record["id"],)).fetchone()
        if old:
            if bytes(old[0]) != body:
                raise ValueError("record_identity_conflict")
            return
        self.db.execute(
            "INSERT INTO records(id,type,run_id,body,created) VALUES(?,?,?,?,?)",
            (record["id"], record["type"], run_id, body, time.time()),
        )

    def append(self, event: dict[str, Any]) -> dict:
        with self.transaction():
            next_seq = self.seq + 1
            value = {
                "schema_version": 2,
                "event_id": uuid.uuid4().hex,
                "producer_id": self.producer_id,
                "producer_epoch": self.epoch,
                "seq": next_seq,
                "ts_ns": time.time_ns(),
                "context": {},
                "payload": {},
                "blob_refs": [],
                "links": [],
                **event,
            }
            record = {"type": "event", "id": value["event_id"], "value": value}
            self._capacity(len(canonical(record)) * 2)
            self._insert(record, value["context"].get("run_id"))
            self.seq = next_seq
        return value

    def records(self, *, run_id: str | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM records ORDER BY ordinal"
                if run_id is None
                else "SELECT body FROM records WHERE run_id=? ORDER BY ordinal",
                () if run_id is None else (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def seal(self, run_id: str, *, coverage: dict | None = None) -> dict:
        with self.transaction():
            events = [
                json.loads(r[0])["value"]
                for r in self.db.execute(
                    "SELECT body FROM records WHERE run_id=? AND type='event' ORDER BY ordinal",
                    (run_id,),
                )
            ]
            if not events:
                raise ValueError("cannot_seal_empty_run")
            ids = [e["event_id"] for e in events]
            manifest = {
                "run_id": run_id,
                "event_ids": ids,
                "events_sha256": digest(canonical(ids)),
                "blob_refs": sorted({r for e in events for r in e.get("blob_refs", [])}),
                "terminal_event_id": ids[-1],
                "coverage": coverage or {},
                "child_run_ids": sorted(
                    {e["payload"]["child_run_id"] for e in events if e["kind"] == "agent.spawn"}
                ),
            }
            record = {"type": "manifest", "id": f"manifest:{run_id}", "value": manifest}
            self._capacity(len(canonical(record)) * 2)
            self._insert(record, run_id)
        return manifest

    def stats(self) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(body)),0),MIN(created) "
                "FROM records WHERE acknowledged=0"
            ).fetchone()
            rejected = self.db.execute(
                "SELECT COUNT(*) FROM batches WHERE state='rejected'"
            ).fetchone()[0]
            gaps = self.db.execute("SELECT value FROM meta WHERE key='capture_gap'").fetchone()
        return {
            "pending_records": row[0],
            "pending_bytes": row[1],
            "oldest_pending": row[2],
            "rejected_batches": rejected,
            "capture_gap": bool(gaps),
        }

    def mark_gap(self) -> None:
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('capture_gap','1')")

    def close(self) -> None:
        with self.lock:
            self.db.close()


class ContentStore:
    def __init__(self, journal: TraceJournal):
        self.journal = journal
        self.root = journal.root / "content"

    def put(self, content: bytes) -> str:
        ref = digest(content)
        path = self.root / ref
        with self.journal.transaction():
            if not os.path.exists(native_path(path)):
                self.journal._capacity(len(content) * 3 + 4096)
                atomic_write(path, content)
            else:
                with open(native_path(path), "rb") as existing:
                    if digest(existing.read()) != ref:
                        raise ValueError("local_content_corruption")
            for offset in range(0, max(len(content), 1), CHUNK_SIZE):
                chunk = content[offset : offset + CHUNK_SIZE]
                record = {
                    "type": "blob",
                    "id": f"{ref}:{offset}",
                    "value": {
                        "sha256": ref,
                        "offset": offset,
                        "total": len(content),
                        "chunk_sha256": digest(chunk),
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                }
                self.journal._insert(record)
        return ref

    def get(self, ref: str) -> bytes:
        if not HEX.fullmatch(ref):
            raise ValueError("invalid_content_reference")
        with open(native_path(self.root / ref), "rb") as stored:
            data = stored.read()
        if digest(data) != ref:
            raise ValueError("content_hash_mismatch")
        return data
