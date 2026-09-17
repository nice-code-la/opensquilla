"""Serialize SQLite WAL/schema initialization across local processes."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def initialization_lock(path: Path, timeout: float = 10):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    deadline = time.monotonic() + timeout
    try:
        while not locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("trace_sqlite_initialization_busy") from None
                time.sleep(0.01)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def initialize(db, path: Path, schema: str):
    with initialization_lock(path.with_suffix(path.suffix + ".init-lock")):
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript(schema)
