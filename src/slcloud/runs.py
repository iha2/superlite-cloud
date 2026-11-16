"""Run access: who may see a run, handing a run to another member, notices."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import members, names
from .capture import rel
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
    if row is None and isinstance(ref, str) and ref:  # "t_fix" with no such id is just a name
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
            moved = db.execute("UPDATE tasks SET owner=?, name=?, updated=? WHERE id=? AND owner=?",
                               (target["id"], name, now, task.id, task.owner)).rowcount
            if moved != 1:  # a concurrent give already moved it: this caller no longer owns the run
                db.rollback()
                raise NotFound("not found")
            db.execute("INSERT INTO events(task,kind,data,at) VALUES(?,?,?,?)",
                       (task.id, "given", json.dumps({"from": task.owner, "to": target["handle"],
                                               "to_email": target["email"], "name": name}), now))
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


def mark_seen_run(store: Store, member: str, task_id: str) -> None:
    """Mark only the notices about one run seen (watching, attaching or nudging it)."""
    store.exec("UPDATE notices SET seen=1 WHERE member=? AND task=?", (member, task_id))


def me(store: Store, who: Principal) -> dict:
    rows = store.query("SELECT handle, email, role FROM members WHERE id=?", (who.member,))
    m = rows[0] if rows else {"handle": getattr(who, "handle", "") or who.member, "email": None, "role": who.role}
    ws = store.query("SELECT name FROM workspaces WHERE id=?", (who.workspace,))
    return {"handle": m["handle"], "email": m["email"], "role": m["role"],
            "workspace": ws[0]["name"] if ws else who.workspace}


def _doing(kind: str, data: dict) -> str | None:
    if kind == "tool":
        text = f"{data.get('tool') or ''} {rel(data.get('target')) or ''}".strip()
    else:
        text = str(data.get("text") or rel(data.get("path")) or rel(data.get("target")) or "").strip()
    return text or None


def live_runs(store: Store, who: Principal) -> list[dict]:
    """The caller's non-terminal runs; step and doing come from the newest captured event that has a step."""
    rows = store.query(
        f"SELECT id, name, status, agent FROM tasks WHERE workspace=? AND owner=? AND status NOT IN {DEAD} "
        "ORDER BY created DESC, rowid DESC", (who.workspace, who.member))
    out = []
    for r in rows:
        step = doing = None
        for ev in store.query("SELECT kind, data FROM events WHERE task=? ORDER BY id DESC LIMIT 30", (r["id"],)):
            try:
                data = json.loads(ev["data"]) if ev["data"] else None
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("step"), int):
                step, doing = data["step"], _doing(ev["kind"], data)
                break
        out.append({"name": r["name"], "status": r["status"], "step": step, "doing": doing, "agent": r["agent"]})
    return out
