"""Team learning: outbox rows, lessons and totals."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from superlite.learn_signals import ROW_FIELDS, accept_row as _accept_row

from slcloud.errors import NotFound, Rejected
from slcloud.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def accept_row(row: dict) -> dict:
    try:
        _accept_row(row)
    except ValueError as e:
        raise Rejected(str(e)) from e
    rid = row.get("id")
    if not isinstance(rid, str) or not rid:
        raise Rejected("row needs an id")
    return dict(row)


def ingest(store: Store, ws: str, member: str, rows: list[dict]) -> dict:
    clean = [accept_row(r) for r in rows]
    accepted = duplicate = 0
    for r in clean:
        n = store.exec(
            "INSERT OR IGNORE INTO learn_rows(workspace,member,rid,signal,row,at) VALUES(?,?,?,?,?,?)",
            (ws, member, r["id"], r.get("signal"), json.dumps(r), _now()),
        )
        if n:
            accepted += 1
        else:
            duplicate += 1
    return {"accepted": accepted, "duplicate": duplicate}


def totals(store: Store, ws: str) -> dict:
    rows = store.query(
        "SELECT signal, COUNT(*) AS n FROM learn_rows WHERE workspace=? AND signal IS NOT NULL GROUP BY signal",
        (ws,),
    )
    return {r["signal"]: r["n"] for r in rows}


def propose(store: Store, ws: str, member: str, text: str) -> dict:
    lid = "l_" + uuid.uuid4().hex[:8]
    store.exec(
        "INSERT INTO lessons(id,workspace,text,proposed_by,status,created) VALUES(?,?,?,?,?,?)",
        (lid, ws, text, member, "held", _now()),
    )
    return {"id": lid, "status": "held", "proposed_by": member}


def _require(store: Store, ws: str, lesson_id: str) -> dict:
    found = store.query("SELECT * FROM lessons WHERE id=? AND workspace=?", (lesson_id, ws))
    if not found:
        raise NotFound(f"lesson {lesson_id} not found")
    return found[0]


def approve(store: Store, ws: str, lesson_id: str, admin: str) -> dict:
    _require(store, ws, lesson_id)
    store.exec(
        "UPDATE lessons SET status='approved', approved_by=? WHERE id=? AND workspace=?",
        (admin, lesson_id, ws),
    )
    return {"id": lesson_id, "status": "approved", "approved_by": admin}


def withdraw(store: Store, ws: str, lesson_id: str) -> dict:
    _require(store, ws, lesson_id)
    store.exec("UPDATE lessons SET status='withdrawn' WHERE id=? AND workspace=?", (lesson_id, ws))
    return {"id": lesson_id, "status": "withdrawn"}


def lessons(store: Store, ws: str) -> list[dict]:
    rows = store.query(
        "SELECT id,text,proposed_by,approved_by FROM lessons WHERE workspace=? AND status='approved' ORDER BY created, rowid",
        (ws,),
    )
    return rows


def outbox_rows(path: str, offset: int) -> tuple[list[dict], int]:
    if not os.path.exists(path):
        return [], offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    out: list[dict] = []
    pos = offset
    while True:
        nl = data.find(b"\n", pos - offset)
        if nl < 0:
            break
        line = data[pos - offset:nl].strip()
        pos = offset + nl + 1
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out, pos
