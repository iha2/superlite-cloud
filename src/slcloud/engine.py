"""Task state engine."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from .adapters import get_adapter
from .errors import BadTransition, NotFound
from .store import Store

ALLOWED = {
    "queued": {"running", "cancelled"},
    "running": {"waiting", "done", "failed", "cancelled"},
    "waiting": {"queued", "cancelled"},
    "done": set(),
    "failed": set(),
    "cancelled": set(),
}
FIELDS = {"session", "plan", "answer", "result", "error"}
PUBLIC = ("id", "repo", "prompt", "agent", "base", "status", "plan", "result", "error", "created", "updated")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    id: str
    ws: str
    repo: str
    prompt: str
    agent: str
    base: str
    status: str
    session: str | None = None
    plan: str | None = None
    answer: str | None = None
    result: object = None
    error: str | None = None
    created: str = ""
    updated: str = ""

    @classmethod
    def from_row(cls, r: dict) -> "Task":
        res = r["result"]
        return cls(r["id"], r["workspace"], r["repo"], r["prompt"], r["agent"], r["base"], r["status"],
                   r["session"], r["plan"], r["answer"], json.loads(res) if res else None,
                   r["error"], r["created"], r["updated"])

    def public(self) -> dict:
        return {k: getattr(self, k) for k in PUBLIC}


class Engine:
    def __init__(self, store: Store, notify=None):
        self.store = store
        self.notify = notify

    def _event(self, task_id: str, status: str) -> None:
        self.store.exec("INSERT INTO events(task,kind,data,at) VALUES(?,?,?,?)",
                        (task_id, "status", json.dumps({"to": status}), _now()))

    def _fire(self, task: Task) -> None:
        if self.notify:
            try:
                self.notify(task)
            except Exception:  # state is already committed; a listener must not break the caller
                logging.getLogger(__name__).exception("notify failed for %s", task.id)

    def _load(self, task_id: str) -> Task:
        rows = self.store.query("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            raise NotFound(f"task {task_id}")
        return Task.from_row(rows[0])

    def submit(self, ws: str, repo: str, prompt: str, agent: str, base: str) -> Task:
        get_adapter(agent)  # raises ValueError for an unknown agent
        tid = "t_" + secrets.token_hex(6)
        now = _now()
        self.store.exec(
            "INSERT INTO tasks(id,workspace,repo,prompt,agent,base,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
            (tid, ws, repo, prompt, agent, base, "queued", now, now))
        self._event(tid, "queued")
        task = self._load(tid)
        self._fire(task)
        return task

    def get(self, ws: str, task_id: str) -> Task:
        task = self._load(task_id)
        if task.ws != ws:
            raise NotFound(f"task {task_id}")
        return task

    def list(self, ws: str) -> list[Task]:
        rows = self.store.query("SELECT * FROM tasks WHERE workspace=? ORDER BY created DESC, rowid DESC", (ws,))
        return [Task.from_row(r) for r in rows]

    def claim(self) -> Task | None:
        with self.store._lock:
            rows = self.store.query(
                "SELECT id FROM tasks WHERE status='queued' ORDER BY created, rowid LIMIT 1")
            if not rows:
                return None
            tid = rows[0]["id"]
            n = self.store.exec("UPDATE tasks SET status='running', updated=? WHERE id=? AND status='queued'",
                                (_now(), tid))
            if not n:
                return None
            self._event(tid, "running")
            task = self._load(tid)
        self._fire(task)
        return task

    def transition(self, task_id: str, status: str, **fields) -> Task:
        bad = set(fields) - FIELDS
        if bad:
            raise ValueError(f"unknown fields: {sorted(bad)}")
        if status not in ALLOWED:
            raise ValueError(f"unknown status: {status}")
        with self.store._lock:
            cur = self._load(task_id)
            if status not in ALLOWED[cur.status]:
                raise BadTransition(f"{cur.status} -> {status}")
            if "result" in fields and fields["result"] is not None:
                fields["result"] = json.dumps(fields["result"])
            cols = ["status=?", "updated=?"] + [f"{k}=?" for k in fields]
            self.store.exec(f"UPDATE tasks SET {', '.join(cols)} WHERE id=? AND status=?",
                            (status, _now(), *fields.values(), task_id, cur.status))
            self._event(task_id, status)
            task = self._load(task_id)
        self._fire(task)
        return task

    def respond(self, ws: str, task_id: str, answer: str) -> Task:
        cur = self.get(ws, task_id)
        if cur.status != "waiting":
            raise BadTransition(f"respond needs waiting, task is {cur.status}")
        return self.transition(task_id, "queued", answer=answer)

    def cancel(self, ws: str, task_id: str) -> Task:
        self.get(ws, task_id)
        return self.transition(task_id, "cancelled")

    def add_event(self, task_id: str, kind: str, data: dict) -> None:
        self._load(task_id)
        self.store.exec("INSERT INTO events(task,kind,data,at) VALUES(?,?,?,?)",
                        (task_id, kind, json.dumps(data), _now()))

    def events(self, task_id: str) -> list[dict]:
        rows = self.store.query("SELECT kind,data,at FROM events WHERE task=? ORDER BY id", (task_id,))
        return [{"kind": r["kind"], "data": json.loads(r["data"]) if r["data"] else None, "at": r["at"]}
                for r in rows]
