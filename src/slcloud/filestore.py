"""task_files table: publish requests, gate sha and edits."""
import json
from datetime import datetime, timezone

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(store, task_id):
    rows = store.query("SELECT * FROM task_files WHERE task_id=?", (task_id,))
    return rows[0] if rows else None


def request_publish(store, task_id: str) -> str:
    with store._lock:
        r = _row(store, task_id)
        if r is None:
            store.exec("INSERT INTO task_files(task_id, state, pushed, bundle, plans, updated) "
                       "VALUES(?,?,0,'','[]',?)", (task_id, "pending", _now()))
            return "pending"
        if r["state"] == "gated":
            store.exec("UPDATE task_files SET state='pending', updated=? WHERE task_id=?",
                       (_now(), task_id))
            return "pending"
        return r["state"]


def pending(store) -> list[str]:
    rows = store.query("SELECT task_id FROM task_files WHERE state='pending' "
                       "ORDER BY updated, rowid")
    return [r["task_id"] for r in rows]


def set_gate(store, task_id: str, branch: str, sha: str, pushed: bool, plans: list[str],
             bundle: str = "") -> None:
    with store._lock:
        r = _row(store, task_id)
        if r is None:
            store.exec("INSERT INTO task_files(task_id, state, branch, sha, pushed, bundle, plans, "
                       "updated) VALUES(?,?,?,?,?,?,?,?)",
                       (task_id, "ready", branch, sha, int(pushed), bundle, json.dumps(plans), _now()))
            return
        keep = plans or json.loads(r["plans"] or "[]")
        store.exec("UPDATE task_files SET state='ready', branch=?, sha=?, pushed=?, bundle=?, "
                   "plans=?, updated=? WHERE task_id=?",
                   (branch, sha, int(pushed), bundle, json.dumps(keep), _now(), task_id))


def set_gate_sha(store, task_id: str, sha: str, plans: list[str]) -> None:
    with store._lock:
        if _row(store, task_id) is None:
            store.exec("INSERT INTO task_files(task_id, state, sha, pushed, bundle, plans, updated) "
                       "VALUES(?,?,?,0,'',?,?)", (task_id, "gated", sha, json.dumps(plans), _now()))
        else:
            store.exec("UPDATE task_files SET sha=?, plans=?, updated=? WHERE task_id=?",
                       (sha, json.dumps(plans), _now(), task_id))


def get(store, task_id: str) -> dict | None:
    r = _row(store, task_id)
    if r is None:
        return None
    edits = None
    if r["edits_sha"]:
        edits = {"sha": r["edits_sha"], "files": json.loads(r["edits_files"] or "[]")}
    return {"task_id": r["task_id"], "state": r["state"], "branch": r["branch"], "sha": r["sha"],
            "pushed": bool(r["pushed"]), "bundle": r["bundle"] or "",
            "plans": json.loads(r["plans"] or "[]"), "edits": edits}


def set_edits(store, task_id: str, sha: str, files: list[str]) -> None:
    with store._lock:
        if _row(store, task_id) is None:
            store.exec("INSERT INTO task_files(task_id, state, pushed, bundle, plans, updated) "
                       "VALUES(?,?,0,'','[]',?)", (task_id, "gated", _now()))
        store.exec("UPDATE task_files SET edits_sha=?, edits_files=?, updated=? WHERE task_id=?",
                   (sha, json.dumps(files), _now(), task_id))


def clear(store, task_id: str) -> None:
    store.exec("DELETE FROM task_files WHERE task_id=?", (task_id,))
