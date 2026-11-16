"""Accounts: invites, passwords, sign-in and web sessions."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from slcloud.store import Store

N_LOG, R, P = 15, 8, 1
MAXMEM = 2**26
MIN_PW = 12
INVITE_TTL = 7 * 86400
SESSION_TTL = 12 * 3600
MAX_MISSES = 5
WAIT = 15 * 60
_LOCAL = {"localhost", "127.0.0.1", "::1"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _scrypt(pw: str, salt: bytes, n_log: int = N_LOG, r: int = R, p: int = P) -> bytes:
    return hashlib.scrypt(pw.encode(), salt=salt, n=2**n_log, r=r, p=p, maxmem=MAXMEM, dklen=64)


def hash_password(pw: str) -> str:
    if len(pw) < MIN_PW:
        raise ValueError(f"password must be at least {MIN_PW} characters")
    salt = os.urandom(16)
    return f"scrypt${N_LOG}${R}${P}${salt.hex()}${_scrypt(pw, salt).hex()}"


def verify(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, key = stored.split("$")
        got = _scrypt(pw, bytes.fromhex(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, bytes.fromhex(key))
    except (ValueError, TypeError):
        return False


DUMMY_HASH = "scrypt$15$8$1$" + "00" * 16 + "$" + "00" * 64


def create_invite(store: Store, member_id: str, now: float) -> str:
    rows = store.query("SELECT workspace FROM members WHERE id=?", (member_id,))
    if not rows:
        raise ValueError("no such member")
    code = secrets.token_urlsafe(24)
    store.exec(
        "INSERT INTO invites(code_hash, workspace, member, expires, used) VALUES(?,?,?,?,0)",
        (_sha(code), rows[0]["workspace"], member_id, now + INVITE_TTL),
    )
    return code


def _live_invite(store: Store, code: str, now: float) -> dict | None:
    rows = store.query(
        "SELECT member FROM invites WHERE code_hash=? AND used=0 AND expires>=?", (_sha(code), now)
    )
    return rows[0] if rows else None


def invite_info(store: Store, code: str, now: float) -> dict | None:
    inv = _live_invite(store, code, now)
    if not inv:
        return None
    rows = store.query("SELECT handle, email FROM members WHERE id=?", (inv["member"],))
    return {"handle": rows[0]["handle"], "email": rows[0]["email"]} if rows else None


def redeem(store: Store, code: str, password: str, now: float) -> str:
    hashed = hash_password(password)  # ValueError before the invite is spent
    inv = _live_invite(store, code, now)
    if not inv:
        raise ValueError("invalid or expired invite")
    n = store.exec(
        "UPDATE invites SET used=1 WHERE code_hash=? AND used=0 AND expires>=?", (_sha(code), now)
    )
    if n != 1:
        raise ValueError("invalid or expired invite")
    store.exec("UPDATE members SET password=?, locked=0 WHERE id=?", (hashed, inv["member"]))
    return inv["member"]


def sign_in(store: Store, handle: str, password: str, now: float) -> str | None:
    handle = handle.strip().lower()
    rows = store.query("SELECT id, password, locked FROM members WHERE handle=?", (handle,))
    m = rows[0] if rows else None
    usable = bool(m and not m["locked"] and m["password"])
    ok = verify(password, m["password"] if usable else DUMMY_HASH)
    with store._lock:  # the slow scrypt above ran unlocked; the lockout read-modify-write is atomic
        return _finish_sign_in(store, handle, m, ok and usable, now)


def _finish_sign_in(store: Store, handle: str, m: dict | None, good: bool, now: float) -> str | None:
    miss = store.query("SELECT count, until FROM sign_in_misses WHERE handle=?", (handle,))
    count, until = (miss[0]["count"], miss[0]["until"]) if miss else (0, 0.0)
    if until > now:
        return None  # waiting: even the right password gets nothing
    if count >= MAX_MISSES:
        count = 0  # wait is over, start a fresh count
    if not good:
        count += 1
        store.exec(
            "INSERT INTO sign_in_misses(handle, count, until) VALUES(?,?,?) "
            "ON CONFLICT(handle) DO UPDATE SET count=excluded.count, until=excluded.until",
            (handle, count, now + WAIT if count >= MAX_MISSES else 0.0),
        )
        return None
    if miss:
        store.exec("DELETE FROM sign_in_misses WHERE handle=?", (handle,))
    sid = secrets.token_urlsafe(32)
    store.exec(
        "INSERT INTO web_sessions(sid_hash, member, expires) VALUES(?,?,?)",
        (_sha(sid), m["id"], now + SESSION_TTL),
    )
    return sid


def session_member(store: Store, sid: str, now: float) -> str | None:
    rows = store.query("SELECT member, expires FROM web_sessions WHERE sid_hash=?", (_sha(sid),))
    if not rows:
        return None
    if rows[0]["expires"] < now:
        store.exec("DELETE FROM web_sessions WHERE sid_hash=?", (_sha(sid),))
        return None
    return rows[0]["member"]


def sign_out(store: Store, sid: str) -> None:
    store.exec("DELETE FROM web_sessions WHERE sid_hash=?", (_sha(sid),))


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return None
    if not u.scheme or not u.hostname:
        return None
    if port is None:
        port = {"http": 80, "https": 443}.get(u.scheme)
    return u.scheme, u.hostname.lower(), port


def origin_ok(origin: str | None, public_url: str) -> bool:
    if not origin:
        return False
    a, b = _origin(origin), _origin(public_url)
    return a is not None and a == b


def check_public_url(url: str, host: str) -> str | None:
    u = urlsplit(url)
    if u.scheme == "http" and (u.hostname or "").lower() not in _LOCAL:
        return "refused: plain http"
    if (u.netloc or "").lower() != host.strip().lower():
        return f"Open {url} instead"
    return None
