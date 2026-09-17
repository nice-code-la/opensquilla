"""Outbox scheduling only: these methods never perform network I/O."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from .journal import TraceJournal
from .protocol import ACK, HEX, MAX_DECODED, MAX_ENCODED, REJECT, canonical, decode, digest, encode


# TracePoint uploads go only to the TokenRhythm platform, the same origins the
# upstream X-OpenSquilla-* correlation headers are restricted to
# (provider/tokenrhythm_correlation.py). A test verifies both sets stay equal.
TOKENRHYTHM_PLATFORM_HOSTS = frozenset({"tokenrhythm.studio", "api.tokenrhythm.studio"})
# Explicit opt-in for local mock platforms and tests, never set in production.
EXTRA_PLATFORM_HOSTS_ENV = "OPENSQUILLA_TRACE_EXTRA_PLATFORM_HOSTS"


def extra_platform_hosts() -> frozenset[str]:
    import os

    raw = os.environ.get(EXTRA_PLATFORM_HOSTS_ENV, "")
    return frozenset(host.strip().lower() for host in raw.split(",") if host.strip())


@dataclass(frozen=True)
class Destination:
    base_url: str
    tenant_id: str
    credential_sha256: str

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not self.tenant_id
            or not HEX.fullmatch(self.credential_sha256)
        ):
            raise ValueError("invalid_trace_destination")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("trace_destination_requires_tls")
        host = url.hostname.lower()
        if host in TOKENRHYTHM_PLATFORM_HOSTS:
            if url.scheme != "https":
                raise ValueError("trace_destination_requires_tls")
        elif host not in extra_platform_hosts():
            raise ValueError("trace_destination_not_tokenrhythm")

    @property
    def key(self) -> str:
        return digest(
            canonical([self.base_url.rstrip("/"), self.tenant_id, self.credential_sha256])
        )

    def matches(self, url: str, headers) -> bool:
        expected, actual = urlsplit(self.base_url), urlsplit(str(url))
        if (
            actual.scheme,
            actual.hostname,
            actual.port or (443 if actual.scheme == "https" else 80),
        ) != (
            expected.scheme,
            expected.hostname,
            expected.port or (443 if expected.scheme == "https" else 80),
        ):
            return False
        prefix = expected.path.rstrip("/")
        if prefix and actual.path != prefix and not actual.path.startswith(prefix + "/"):
            return False
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        credential = lowered.get("authorization", "")
        if credential.lower().startswith("bearer "):
            credential = credential[7:]
        else:
            credential = lowered.get("x-api-key", lowered.get("api-key", ""))
        return bool(credential) and digest(credential.encode()) == self.credential_sha256


class PiggybackTransport:
    def __init__(
        self, journal: TraceJournal, *, lease_seconds: float = 120, execution_version: int = 3
    ):
        if type(execution_version) is not int or execution_version not in {3, 4}:
            raise ValueError("invalid_execution_version")
        self.journal = journal
        self.lease_seconds = lease_seconds
        self.execution_version = execution_version
        with journal.transaction():
            journal.db.execute(
                "CREATE TABLE IF NOT EXISTS batch_members("
                "batch_id TEXT, record_id TEXT, representation TEXT, "
                "start INTEGER, stop INTEGER, total INTEGER, "
                "PRIMARY KEY(batch_id,record_id,start,stop))"
            )
            journal.db.execute(
                "CREATE INDEX IF NOT EXISTS batch_member_record "
                "ON batch_members(record_id,representation)"
            )
            # Migrate old immutable envelopes once, preserving late receipt membership.
            for row in journal.db.execute(
                "SELECT id,envelope FROM batches WHERE id NOT IN "
                "(SELECT batch_id FROM batch_members)"
            ).fetchall():
                for record in decode(json.loads(row["envelope"])):
                    journal.db.execute(
                        "INSERT OR IGNORE INTO batch_members VALUES(?,?,?,?,?,?)",
                        self._member(row["id"], record),
                    )

    @staticmethod
    def _member(batch_id, record):
        if record["type"] == "record_fragment":
            v = record["value"]
            return (
                batch_id,
                v["record_id"],
                v["record_sha256"],
                v["offset"],
                v["offset"] + v["length"],
                v["total"],
            )
        raw = canonical(record)
        return batch_id, record["id"], digest(raw), 0, len(raw), len(raw)

    def prepare(self, destination: Destination, budget: int = MAX_ENCODED) -> dict | None:
        from .fragments import fragment, uncovered

        if destination.key != self.journal.destination:
            return None
        budget = min(budget, MAX_ENCODED)
        if budget < 1024:
            return None
        now = time.time()
        with self.journal.transaction():
            db = self.journal.db
            for row in db.execute(
                "SELECT * FROM batches WHERE state='pending' AND lease_until<=? ORDER BY created",
                (now,),
            ).fetchall():
                needed = db.execute(
                    "SELECT 1 FROM batch_members m JOIN records r ON r.id=m.record_id "
                    "WHERE m.batch_id=? AND r.acknowledged=0 LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if needed and len(row["envelope"]) <= budget:
                    db.execute(
                        "UPDATE batches SET lease_until=? WHERE id=?",
                        (now + self.lease_seconds, row["id"]),
                    )
                    return json.loads(row["envelope"])
                # Keep the original bytes and membership for late ACKs. A new
                # envelope can now carry unconfirmed records or smaller ranges.
                db.execute("UPDATE batches SET state='superseded' WHERE id=?", (row["id"],))
            queues = []
            for predicate in ("r.type!='blob'", "r.type='blob'"):
                queues.append(
                    list(
                        db.execute(
                            "SELECT r.id,r.body FROM records r WHERE r.acknowledged=0 AND "
                            "NOT EXISTS (SELECT 1 FROM batch_members m JOIN batches b "
                            "ON b.id=m.batch_id "
                            "WHERE m.record_id=r.id AND b.state IN ('pending','rejected')) "
                            f"AND {predicate} ORDER BY r.ordinal LIMIT 4096"
                        )
                    )
                )
            selected, sizes, blocked = [], [0, 0], [False, False]
            batch_id, raw_size, envelope = uuid.uuid4().hex, 0, None
            while any(queues[i] and not blocked[i] for i in (0, 1)):
                group = min(
                    (i for i in (0, 1) if queues[i] and not blocked[i]), key=lambda i: sizes[i]
                )
                row = queues[group].pop(0)
                record = json.loads(row["body"])
                if self.execution_version == 4:
                    from .identity_facts import wire_record

                    record = wire_record(record)
                raw = canonical(record)
                intervals = [
                    (r[0], r[1])
                    for r in db.execute(
                        "SELECT m.start,m.stop FROM batch_members m JOIN batches b "
                        "ON b.id=m.batch_id "
                        "WHERE m.record_id=? AND m.representation=? AND b.state='acked'",
                        (record["id"], digest(raw)),
                    )
                ]
                remaining = uncovered(intervals, len(raw))
                if remaining is None:
                    db.execute("UPDATE records SET acknowledged=1 WHERE id=?", (record["id"],))
                    continue
                start, stop = remaining
                candidate = None
                if start == 0 and stop == len(raw) and raw_size + len(raw) < MAX_DECODED - 4096:
                    trial = encode(self.journal.source_id, batch_id, selected + [record])
                    if len(canonical(trial)) <= budget:
                        candidate = trial
                if candidate is None:
                    # Binary search a fitting fragment of the ORIGINAL record.
                    # Never mutate hash:offset blob records to smaller chunks.
                    from .protocol import CHUNK_SIZE

                    lo, hi, fit = (
                        1,
                        min(stop - start, CHUNK_SIZE, MAX_DECODED - 4096 - raw_size),
                        None,
                    )
                    while lo <= hi:
                        length = (lo + hi) // 2
                        piece = fragment(record, start, length)
                        trial = encode(self.journal.source_id, batch_id, selected + [piece])
                        if (
                            len(canonical(trial)) <= budget
                            and raw_size + len(canonical(piece)) < MAX_DECODED - 4096
                        ):
                            fit = piece, trial
                            lo = length + 1
                        else:
                            hi = length - 1
                    if fit is None:
                        blocked[group] = True
                        continue
                    record, candidate = fit
                    # A later business request continues the remaining range.
                    blocked[group] = True
                size = len(canonical(record))
                selected.append(record)
                raw_size += size
                sizes[group] += size
                envelope = candidate
            if envelope is None:
                return None
            encoded = canonical(envelope)
            self.journal._capacity(len(encoded) + len(selected) * 512)
            db.execute(
                "INSERT INTO batches VALUES(?,?,?,?,?)",
                (batch_id, encoded, "pending", now + self.lease_seconds, now),
            )
            members = [self._member(batch_id, r) for r in selected]
            db.executemany("INSERT INTO batch_members VALUES(?,?,?,?,?,?)", members)
            # Compatibility cursor only: receipt coverage comes from batch_members.
            db.executemany(
                "UPDATE records SET batch_id=? WHERE id=?", [(batch_id, m[1]) for m in members]
            )
            return envelope

    def release(self, batch_id: str) -> None:
        # A business call finished without a receipt. Requeue locally; never send here.
        with self.journal.transaction():
            self.journal.db.execute(
                "UPDATE batches SET lease_until=0 WHERE id=? AND state='pending'", (batch_id,)
            )

    def confirm(self, response_headers, *, batch_id: str) -> bool:
        headers = {str(k).lower(): str(v) for k, v in response_headers.items()}
        with self.journal.transaction():
            db = self.journal.db
            if headers.get(ACK) == batch_id:
                row = db.execute("SELECT state FROM batches WHERE id=?", (batch_id,)).fetchone()
                if not row or row[0] == "rejected":
                    return False
                db.execute("UPDATE batches SET state='acked' WHERE id=?", (batch_id,))
                from .fragments import uncovered

                for member in db.execute(
                    "SELECT record_id,representation,total FROM batch_members WHERE batch_id=?",
                    (batch_id,),
                ).fetchall():
                    intervals = [
                        (r[0], r[1])
                        for r in db.execute(
                            "SELECT m.start,m.stop FROM batch_members m JOIN batches b "
                            "ON b.id=m.batch_id WHERE m.record_id=? AND m.representation=? "
                            "AND b.state='acked'",
                            (member[0], member[1]),
                        )
                    ]
                    if uncovered(intervals, member[2]) is None:
                        db.execute("UPDATE records SET acknowledged=1 WHERE id=?", (member[0],))
                return True
            if headers.get(REJECT, "").split(":", 1)[0] == batch_id:
                db.execute(
                    "UPDATE batches SET state='rejected' WHERE id=? AND state='pending'",
                    (batch_id,),
                )
                db.execute("INSERT OR REPLACE INTO meta VALUES('capture_gap','1')")
        return False

    # Identity kinds that later Runs resolve by reference and that are therefore
    # never pruned automatically: session anchors and turn chains, the Run and its
    # dispatch receipts (background children close after the parent), committed
    # message versions and results (persisted history, adopted child output),
    # Calls, and ensemble candidates reused by a later round. Everything else is
    # only referenced inside its own sealed Run (structure, terminals, closures).
    _ANCHOR_KINDS = (
        "session",
        "authority",
        "branch",
        "turn",
        "input",
        "native_input",
        "checkpoint",
        "run",
        "activation",
        "input_group",
        "dispatch",
        "dispatch_completion",
        "dispatch_cancelled",
        "message",
        "result",
        "acceptance",
        "commit",
        "call",
        "ensemble",
        "candidate",
        "candidate_result",
        "projection",
        "selection",
        "aggregation",
    )

    def prune_acknowledged(self, before: float, *, keep_open_scopes: bool = True) -> int:
        """Local maintenance. Never delete an unconfirmed record.

        Deletes acknowledged events, bodies and legacy records older than ``before``;
        identity declarations of sealed Runs are deleted too, except cross-run anchors.
        Content files are removed once no blob record references them.
        """
        import os

        from .journal import native_path

        with self.journal.transaction():
            db = self.journal.db
            count = db.execute(
                "DELETE FROM records WHERE acknowledged=1 AND created<? AND type!='id_node'",
                (before,),
            ).rowcount
            anchors = " AND ".join(f"id NOT LIKE '%:{k}:%'" for k in self._ANCHOR_KINDS)
            scope_filter = (
                " AND run_id IN (SELECT id FROM identity_scopes WHERE closed=1)"
                if keep_open_scopes
                else ""
            )
            count += db.execute(
                "DELETE FROM records WHERE acknowledged=1 AND created<? AND type='id_node' "
                f"AND {anchors}{scope_filter}",
                (before,),
            ).rowcount
            # Coverage rows are only needed while their record is still unconfirmed.
            db.execute(
                "DELETE FROM batch_members WHERE batch_id IN "
                "(SELECT id FROM batches WHERE state='acked') AND ("
                "record_id NOT IN (SELECT id FROM records) OR "
                "record_id IN (SELECT id FROM records WHERE acknowledged=1))"
            )
            db.execute(
                "DELETE FROM batches WHERE state='acked' AND id NOT IN "
                "(SELECT batch_id FROM batch_members)"
            )
            db.execute("DELETE FROM batch_members WHERE batch_id NOT IN (SELECT id FROM batches)")
            # Closure inventories were consumed when the scope was sealed.
            db.execute(
                "DELETE FROM identity_members WHERE scope_id IN "
                "(SELECT id FROM identity_scopes WHERE closed=1)"
            )
            referenced = {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT substr(id,1,64) FROM records WHERE type='blob'"
                )
            }
            content = self.journal.content.root
            if os.path.isdir(native_path(content)):
                for name in os.listdir(native_path(content)):
                    if len(name) == 64 and name not in referenced:
                        try:
                            os.unlink(native_path(content / name))
                        except FileNotFoundError:
                            pass
        if count:
            # Return the space to the filesystem: fold the WAL back and compact.
            # Cheap here because a pruned journal only holds the unconfirmed tail
            # and small cross-run anchors; never inside the transaction above.
            with self.journal.lock:
                self.journal.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                now = time.time()
                if now - getattr(self, "_last_vacuum", 0) >= 60:
                    self.journal.db.execute("VACUUM")
                    self._last_vacuum = now
        return count
