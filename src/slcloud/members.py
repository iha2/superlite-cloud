"""Member rows: handles, emails, roles, lock."""
from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from slcloud.errors import HandleTaken

if TYPE_CHECKING:
    from slcloud.store import Store

ROLES = ("admin", "member")
_HANDLE = re.compile(r"[a-z0-9-]+")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_COLS = "id, workspace, handle, email, role, locked"


def _handle(h: str) -> str:
    if not isinstance(h, str) or not _HANDLE.fullmatch(h):
        raise ValueError("handle must be a-z, 0-9 and -")
    return h


def _email(e: str) -> str:
    e = (e or "").strip().lower()
    if not _EMAIL.fullmatch(e):
        raise ValueError("invalid email")
    return e


def _role(r: str) -> str:
    if r not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    return r


def _get(store: Store, member_id: str) -> dict:
    rows = store.query(f"SELECT {_COLS} FROM members WHERE id=?", (member_id,))
    if not rows:
        raise ValueError(f"no such member: {member_id}")
    return rows[0]


def handle_taken(store: Store, handle: str) -> bool:
    return bool(store.query("SELECT 1 FROM members WHERE handle=?", (handle,)))


def create_member(store: Store, workspace: str, handle: str, email: str | None, role: str) -> str:
    _handle(handle)
    _role(role)
    email = _email(email) if email is not None else None
    if handle_taken(store, handle):
        raise HandleTaken(handle)
    mid = "m_" + secrets.token_hex(4)
    try:
        store.exec("INSERT INTO members(id,workspace,handle,email,role,created) VALUES(?,?,?,?,?,?)",
                   (mid, workspace, handle, email, role, datetime.now(timezone.utc).isoformat()))
    except sqlite3.IntegrityError as e:
        if handle_taken(store, handle):
            raise HandleTaken(handle) from e
        raise ValueError("email already in use") from e
    return mid


def member_by_email(store: Store, email: str) -> dict | None:
    rows = store.query(f"SELECT {_COLS} FROM members WHERE email=?", ((email or "").strip().lower(),))
    return rows[0] if rows else None


def member_by_handle(store: Store, workspace: str, handle: str) -> dict | None:
    rows = store.query(f"SELECT {_COLS} FROM members WHERE workspace=? AND handle=?", (workspace, handle))
    return rows[0] if rows else None


def list_members(store: Store, workspace: str) -> list[dict]:
    return store.query("SELECT id, handle, email, role, locked FROM members WHERE workspace=? ORDER BY handle",
                       (workspace,))


def rename(store: Store, member_id: str, handle: str) -> None:
    _handle(handle)
    _get(store, member_id)
    try:
        store.exec("UPDATE members SET handle=? WHERE id=?", (handle, member_id))
    except sqlite3.IntegrityError as e:
        raise HandleTaken(handle) from e


def set_email(store: Store, member_id: str, email: str) -> None:
    email = _email(email)
    _get(store, member_id)
    try:
        store.exec("UPDATE members SET email=? WHERE id=?", (email, member_id))
    except sqlite3.IntegrityError as e:
        raise ValueError("email already in use") from e


def set_role(store: Store, member_id: str, role: str) -> None:
    _role(role)
    _get(store, member_id)
    with store._lock:
        store.exec("UPDATE members SET role=? WHERE id=?", (role, member_id))
        store.exec("UPDATE tokens SET role=? WHERE member=?", (role, member_id))


def lock(store: Store, member_id: str) -> None:
    _get(store, member_id)
    with store._lock:
        store.exec("DELETE FROM tokens WHERE member=?", (member_id,))
        store.exec("DELETE FROM web_sessions WHERE member=?", (member_id,))
        store.exec("UPDATE members SET password=NULL, locked=1 WHERE id=?", (member_id,))
