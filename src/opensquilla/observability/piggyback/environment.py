"""Task-scoped filesystem/SQLite snapshots and explicit external-state capability grades."""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import platform
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .journal import atomic_write
from .protocol import HEX, canonical, digest, validate_record

MARKER = ".tracepoint-owned"
DEFAULT_EXCLUDES = (".venv", "node_modules", "__pycache__", ".env", ".env.*", "*.pem", "*.key")


class RecordedContentStore:
    """Read verified received records without access to the original host or network."""

    def __init__(self, records: list[dict]):
        self.chunks = {}
        seen = {}
        for record in records:
            if record.get("type") != "blob":
                continue
            validate_record(record)
            key, body = record["id"], canonical(record)
            if key in seen:
                if seen[key] != body:
                    raise ValueError("conflicting_content_record")
                continue
            seen[key] = body
            value = record["value"]
            self.chunks.setdefault(value["sha256"], []).append(value)

    def get(self, ref: str) -> bytes:
        if not HEX.fullmatch(ref):
            raise ValueError("invalid_content_reference")
        if ref not in self.chunks:
            raise FileNotFoundError("content_not_received:" + ref)
        offset, parts, total = 0, [], None
        for piece in sorted(self.chunks[ref], key=lambda p: p["offset"]):
            if total is None:
                total = piece["total"]
            if total != piece["total"] or piece["offset"] != offset:
                raise ValueError("content_incomplete_or_conflicting:" + ref)
            data = base64.b64decode(piece["data"])
            parts.append(data)
            offset += len(data)
        if offset != total:
            raise ValueError("content_incomplete:" + ref)
        result = b"".join(parts)
        if digest(result) != ref:
            raise ValueError("content_hash_mismatch:" + ref)
        return result


def safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("unsafe_snapshot_path")
    if path.parts[0] == MARKER:
        raise ValueError("reserved_snapshot_path")
    return path


def is_owned(root: Path) -> bool:
    marker = root / MARKER
    try:
        value = json.loads(marker.read_text()) if not marker.is_symlink() else {}
        return value.get("format") == "opensquilla.environment.v1" and bool(
            HEX.fullmatch(value.get("snapshot_id", ""))
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return False


class FileEnvironmentAdapter:
    def __init__(self, content_store, *, excludes=DEFAULT_EXCLUDES, max_file_bytes=64 * 1024**2):
        self.content = content_store
        self.excludes, self.max_file_bytes = tuple(excludes), max_file_bytes

    def capture(self, root: Path) -> dict:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("snapshot_requires_directory")
        entries, gaps, excluded = [], [], []
        for directory, directories, files in os.walk(root, followlinks=False):
            directories.sort()
            files.sort()
            for name in list(directories) + files:
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if name == MARKER or any(
                    fnmatch.fnmatch(name, pattern) for pattern in self.excludes
                ):
                    excluded.append(relative)
                    if name in directories:
                        directories.remove(name)
                    continue
                try:
                    info = path.lstat()
                    entry = {"path": relative, "mode": stat.S_IMODE(info.st_mode)}
                    if entry["mode"] & 0o7000:
                        gaps.append({"path": relative, "reason": "special_permission_bits"})
                    if path.is_symlink():
                        target = os.readlink(path)
                        if Path(target).is_absolute() or not path.resolve().is_relative_to(root):
                            gaps.append({"path": relative, "reason": "external_symlink"})
                            continue
                        entry.update(type="symlink", target=target)
                    elif path.is_dir():
                        entry.update(type="directory")
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_size > self.max_file_bytes:
                            gaps.append({"path": relative, "reason": "file_size_limit"})
                            continue
                        with path.open("rb") as f:
                            header = f.read(16)
                        if header == b"SQLite format 3\x00":
                            data = self._sqlite_snapshot(path)
                            entry["sqlite_backup"] = True
                        else:
                            data = path.read_bytes()
                            after = path.stat()
                            if (info.st_mtime_ns, info.st_size, info.st_ino) != (
                                after.st_mtime_ns,
                                after.st_size,
                                after.st_ino,
                            ):
                                gaps.append({"path": relative, "reason": "concurrent_mutation"})
                        entry.update(type="file", sha256=self.content.put(data), bytes=len(data))
                    else:
                        gaps.append({"path": relative, "reason": "unsupported_file_type"})
                        continue
                    entries.append(entry)
                except (OSError, ValueError) as exc:
                    gaps.append({"path": relative, "reason": type(exc).__name__})
        # WAL/SHM are not part of a backup API snapshot; including them would corrupt restoration.
        sqlite_paths = {e["path"] for e in entries if e.get("sqlite_backup")}
        entries = [
            e
            for e in entries
            if not any(e["path"] == p + suffix for p in sqlite_paths for suffix in ("-wal", "-shm"))
        ]
        snapshot = {
            "version": 1,
            "adapter": "files-sqlite-v1",
            "source_root": str(root),
            "entries": sorted(entries, key=lambda e: e["path"]),
            "gaps": gaps,
            "excluded": excluded,
            "capabilities": ["files", "sqlite"],
            "runtime": {
                "os": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "grade": "incomplete" if gaps else "replay_only" if excluded else "executable",
        }
        snapshot["blob_refs"] = sorted({e["sha256"] for e in entries if "sha256" in e})
        snapshot["snapshot_id"] = digest(canonical(snapshot))
        return snapshot

    def _sqlite_snapshot(self, path: Path) -> bytes:
        with tempfile.TemporaryDirectory(prefix="trace-sqlite-") as temporary:
            target = Path(temporary) / "snapshot.sqlite3"
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(target) as destination:
                    source.backup(destination)
            return target.read_bytes()

    def restore(self, snapshot: dict, target: Path) -> Path:
        expected = digest(canonical({k: v for k, v in snapshot.items() if k != "snapshot_id"}))
        if expected != snapshot.get("snapshot_id"):
            raise ValueError("snapshot_hash_mismatch")
        target = target.absolute()
        resolved, original = target.resolve(), Path(snapshot["source_root"]).resolve()
        if (
            target.is_symlink()
            or resolved.is_relative_to(original)
            or original.is_relative_to(resolved)
        ):
            raise ValueError("restore_requires_isolated_target")
        if target.exists() and any(target.iterdir()) and not is_owned(target):
            raise ValueError("restore_requires_empty_or_owned_target")
        entries = snapshot["entries"]
        paths = [safe_relative(e["path"]) for e in entries]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate_snapshot_path")
        symlinks = {safe_relative(e["path"]) for e in entries if e["type"] == "symlink"}
        for path in paths:
            if any(parent in symlinks for parent in path.parents):
                raise ValueError("snapshot_symlink_ancestor")
        # Validate all content before any target mutation.
        for entry in entries:
            if entry["type"] == "file":
                if len(self.content.get(entry["sha256"])) != entry["bytes"]:
                    raise ValueError("snapshot_content_size")
            elif entry["type"] not in {"directory", "symlink"}:
                raise ValueError("unknown_snapshot_entry")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".trace-restore-", dir=target.parent))
        try:
            for entry in sorted(
                entries, key=lambda e: (e["type"] == "symlink", len(PurePosixPath(e["path"]).parts))
            ):
                path = staging / entry["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                if entry["type"] == "directory":
                    path.mkdir(exist_ok=True)
                elif entry["type"] == "file":
                    atomic_write(path, self.content.get(entry["sha256"]))
                    path.chmod(entry["mode"] & 0o777)
                else:
                    link = Path(entry["target"])
                    if link.is_absolute() or not (path.parent / link).resolve().is_relative_to(
                        staging
                    ):
                        raise ValueError("unsafe_snapshot_symlink")
                    path.symlink_to(entry["target"])
            atomic_write(
                staging / MARKER,
                canonical(
                    {"format": "opensquilla.environment.v1", "snapshot_id": snapshot["snapshot_id"]}
                ),
            )
            # Apply directory modes after descendants have been created.
            for entry in sorted(
                entries, key=lambda e: len(PurePosixPath(e["path"]).parts), reverse=True
            ):
                if entry["type"] == "directory":
                    (staging / entry["path"]).chmod(entry["mode"] & 0o777)
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return target

    def reset(self, snapshot: dict, target: Path) -> Path:
        if not is_owned(target):
            raise ValueError("reset_requires_owned_target")
        return self.restore(snapshot, target)

    def verify(self, snapshot: dict, target: Path) -> dict:
        failures = []
        expected = {e["path"] for e in snapshot["entries"]}
        actual = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.name != MARKER}
        if actual != expected:
            failures.append("path_set_mismatch")
        for entry in snapshot["entries"]:
            path = target / safe_relative(entry["path"])
            if entry["type"] != "symlink" and path.exists():
                if stat.S_IMODE(path.stat().st_mode) != entry["mode"]:
                    failures.append(entry["path"] + ":mode")
            if entry["type"] == "file":
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or digest(path.read_bytes()) != entry["sha256"]
                ):
                    failures.append(entry["path"])
            elif entry["type"] == "symlink":
                if not path.is_symlink() or os.readlink(path) != entry["target"]:
                    failures.append(entry["path"])
            elif not path.is_dir() or path.is_symlink():
                failures.append(entry["path"])
        return {
            "ok": not failures,
            "failures": sorted(failures),
            "capabilities": snapshot["capabilities"],
        }

    @staticmethod
    def diff(before: dict, after: dict) -> dict:
        old = {e["path"]: e for e in before["entries"]}
        new = {e["path"]: e for e in after["entries"]}
        return {
            "created": sorted(new.keys() - old.keys()),
            "deleted": sorted(old.keys() - new.keys()),
            "changed": sorted(k for k in new.keys() & old.keys() if new[k] != old[k]),
            "before": before["snapshot_id"],
            "after": after["snapshot_id"],
        }


class RecordedServiceAdapter:
    """Exact recorded-call replay; new requests require an explicit stateful adapter."""

    grade = "replay_only"

    def __init__(self, observations: list[dict]):
        self.observations = observations
        self.position = 0

    def call(self, request: dict):
        if self.position >= len(self.observations):
            raise ValueError("unrecorded_external_request")
        row = self.observations[self.position]
        if canonical(request) != canonical(row["request"]):
            raise ValueError("external_replay_divergence")
        self.position += 1
        return row["response"]

    def reset(self):
        self.position = 0

    @classmethod
    def from_har(cls, har: dict):
        return cls(
            [
                {"request": entry["request"], "response": entry["response"]}
                for entry in har["log"]["entries"]
            ]
        )
