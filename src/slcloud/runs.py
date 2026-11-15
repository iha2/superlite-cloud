"""Run access: who may see a run, handing a run to another member, notices."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import members, names
from .errors import NotFound

if TYPE_CHECKING:
    from slcloud.auth import Principal
    from slcloud.engine import Engine, Task
    from slcloud.store import Store

DEAD = "('done','failed','cancelled')"


class NoSuchMember(Exception):
    """The email names nobody who can receive a run (HTTP 404 {"error": "no such member"})."""


def _task(row: dict) -> "Task":
    from slcloud.engine import Task
    return Task.from_row(row)


def _pick(store: Store, ws: str, owner: str, name: str) -> dict | None:
    rows = store.query(
        "SELECT * FROM tasks WHERE workspace=? AND owner=? AND name=? "
        "ORDER BY (status IN ('done','failed','cancelled')), created DESC, rowid DESC LIMIT 1",
        (ws, owner, name))
    return rows[0] if rows else None


def get_run(store: Store, who: Principal, ref: str) -> Task:
    """Resolve "name", "handle/name" or a task id; anything the caller may not see is NotFound."""
    row = None
    if isinstance(ref, str) and ref.startswith("t_"):
        rows = store.query("SELECT * FROM tasks WHERE id=? AND workspace=?", (ref, who.workspace))
        row = rows[0] if rows else None
    elif isinstance(ref, str) and ref:
        if "/" in ref:
            handle, _, name = ref.partition("/")
            m = members.member_by_handle(store, who.workspace, handle)
            row = _pick(store, who.workspace, m["id"], name) if m else None
        else:
            row = _pick(store, who.workspace, who.member, ref)
    if row is None or (row["owner"] != who.member and who.role != "admin"):
        raise NotFound("not found")
    return _task(row)


def _handle_of(store: Store, member_id: str) -> str:
    rows = store.query("SELECT handle FROM members WHERE id=?", (member_id,))
    return rows[0]["handle"] if rows else member_id


def give(store: Store, engine: Engine, who: Principal, ref: str, email: str) -> Task:
    task = get_run(store, who, ref)
    target = members.member_by_email(store, email) if isinstance(email, str) and "@" in email else None
    if not target or target["workspace"] != who.workspace or target["locked"]:
        raise NoSuchMember("no such member")
    if target["id"] == task.owner:
        return task
    now = datetime.now(timezone.utc).isoformat()
    with store._lock:
        live = store.query(
            f"SELECT name FROM tasks WHERE workspace=? AND repo IS ? AND id!=? AND status NOT IN {DEAD}",
            (task.ws, task.repo, task.id))
        name = names.free_name(task.name, {r["name"] for r in live})
        giver = _handle_of(store, who.member)
        text = f"given by {giver} to {target['handle']} ({target['email']})"
        db = store._db
        try:
            db.execute("UPDATE tasks SET owner=?, name=?, updated=? WHERE id=?",
                       (target["id"], name, now, task.id))
            db.execute("INSERT INTO events(task,kind,data,at) VALUES(?,?,?,?)",
                       (task.id, "given", json.dumps({"from": task.owner, "to": target["id"], "name": name}), now))
            db.execute("INSERT INTO notices(member,task,by_member,text,seen,at) VALUES(?,?,?,?,0,?)",
                       (target["id"], task.id, who.member, text, now))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return engine._load(task.id)


def notices(store: Store, member: str) -> list[dict]:
    return store.query("SELECT id, task, text, seen FROM notices WHERE member=? ORDER BY id", (member,))


def mark_seen(store: Store, member: str) -> None:
    store.exec("UPDATE notices SET seen=1 WHERE member=?", (member,))
