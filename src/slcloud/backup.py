"""Database backup and restore using the sqlite3 backup API."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import tempfile
from collections.abc import Callable


class Refused(Exception):
    """A restore the server state or the source file does not allow."""


def _lock_path(db_path: str) -> str:
    return db_path + ".maint"


def _open_lock(db_path: str):
    fd = os.open(_lock_path(db_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "a+")


def hold_shared(db_path: str):
    """Hold the maintenance lock shared for as long as the returned file stays open.

    The server and the worker call this once at start-up and keep the result for the life
    of the process. restore() needs the exclusive lock, so it is refused while either runs,
    even from another container that shares the data directory.
    """
    fh = _open_lock(db_path)
    try:
        fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise Refused("a restore is in progress") from None
    return fh


def _temp(base: str, suffix: str) -> str:
    """Create a fresh 0600 file next to base. Never reuses or follows an existing path."""
    base = os.path.abspath(base)
    fd, path = tempfile.mkstemp(prefix=os.path.basename(base) + ".", suffix=suffix,
                                dir=os.path.dirname(base))
    os.close(fd)
    return path


def _copy(src: str, dest: str) -> None:
    s = sqlite3.connect(src)
    d = sqlite3.connect(dest)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
        d.commit()
    finally:
        d.close()
        s.close()


def backup(db_path: str, out: str) -> None:
    if not os.path.exists(db_path):
        raise FileNotFoundError("database not found")
    tmp = _temp(out, ".tmp")
    try:
        _copy(db_path, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def restore(db_path: str, src: str, server_running: Callable[[], bool]) -> None:
    if server_running():
        raise Refused("stop the server before restoring")
    lock = _open_lock(db_path)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("stop the server and worker before restoring") from None
        _restore_locked(db_path, src)
    finally:
        lock.close()


def _restore_locked(db_path: str, src: str) -> None:
    if not os.path.isfile(src):
        raise Refused("backup file not found")
    try:
        c = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            c.close()
    except sqlite3.DatabaseError:
        ok = False
    if not ok:
        raise Refused("not a valid backup")
    tmp = _temp(db_path, ".restore")
    try:
        _copy(src, tmp)
        os.chmod(tmp, 0o600)
        for suffix in ("-wal", "-shm"):  # stale journals would replay over the restored file
            if os.path.exists(db_path + suffix):
                os.unlink(db_path + suffix)
        os.replace(tmp, db_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
