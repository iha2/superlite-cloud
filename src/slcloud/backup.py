"""Database backup and restore using the sqlite3 backup API."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable


class Refused(Exception):
    """A restore the server state or the source file does not allow."""


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
    tmp = f"{out}.{os.getpid()}.tmp"
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
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
    tmp = f"{db_path}.{os.getpid()}.restore"
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
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
