import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from slcloud import learning
from slcloud.api import create_app
from slcloud.config import Settings
from slcloud.errors import BadTransition, NotFound, Rejected
from slcloud.store import Store
from slcloud.webhooks import Notifier, sign

PUB = {"id": "t_0123456789ab", "repo": "r", "prompt": "p", "agent": "claude", "base": "HEAD",
       "status": "queued", "plan": None, "result": None, "error": None, "created": "c", "updated": "u"}


class FakeTask:
    workspace = "w1"

    def __init__(self, **kw):
        self.d = {**PUB, **kw}

    def public(self):
        return self.d


class FakeEngine:
    def submit(self, ws, repo, prompt, agent, base):
        return FakeTask(repo=repo, prompt=prompt, agent=agent, base=base)

    def get(self, ws, tid):
        if ws != "w1" or tid != PUB["id"]:
            raise NotFound(tid)
        return FakeTask()

    def list(self, ws):
        return [FakeTask()]

    def respond(self, ws, tid, answer):
        self.get(ws, tid)
        raise BadTransition("not waiting")

    def cancel(self, ws, tid):
        return FakeTask(status="cancelled")

    def events(self, tid):
        return [{"kind": "status", "data": {"to": "queued"}, "at": "t"}]


def client(role="admin", store=None):
    store = store or Store(":memory:")
    s = Settings("/d", "/d/x.db", "/d/work", "k")
    who = lambda: SimpleNamespace(workspace="w1", role=role, member="m1")
    return TestClient(create_app(s, store, FakeEngine(), who)), store


def test_health_and_task_routes():
    c, _ = client()
    assert c.get("/api/health").json() == {"ok": True}
    r = c.post("/api/tasks", json={"repo": "r", "prompt": "p"})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert c.get("/api/tasks").json() == [PUB]
    assert c.get(f"/api/tasks/{PUB['id']}").json() == PUB
    assert c.post(f"/api/tasks/{PUB['id']}/cancel").json()["status"] == "cancelled"
    assert c.get(f"/api/tasks/{PUB['id']}/events").json()[0]["kind"] == "status"
    assert c.get(f"/api/tasks/{PUB['id']}/bundle").status_code == 404


def test_not_found_404():
    c, _ = client()
    r = c.get("/api/tasks/t_nope")
    assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_bad_transition_409():
    c, _ = client()
    assert c.post(f"/api/tasks/{PUB['id']}/respond", json={"answer": "approve"}).status_code == 409


def test_member_cannot_approve():
    c, _ = client("member")
    assert c.post("/api/lessons/l_1/approve").status_code == 403
    assert c.post("/api/lessons/l_1/withdraw").status_code == 403
    assert c.post("/api/webhooks", json={"url": "http://x", "secret": "s"}).status_code == 403


def test_admin_approve(monkeypatch):
    monkeypatch.setattr(learning, "approve", lambda s, ws, i, a: {"id": i, "status": "approved", "approved_by": a})
    c, _ = client()
    assert c.post("/api/lessons/l_1/approve").json() == {"id": "l_1", "status": "approved", "approved_by": "m1"}


def test_rejected_row_400(monkeypatch):
    def bad(s, ws, m, rows):
        raise Rejected("unexpected fields: file, path")
    monkeypatch.setattr(learning, "ingest", bad)
    c, _ = client()
    r = c.post("/api/learn/rows", json={"rows": [{"file": "x", "path": "y"}]})
    assert r.status_code == 400
    assert "file" in r.json()["error"] and "path" in r.json()["error"]


def test_learn_and_lessons(monkeypatch):
    monkeypatch.setattr(learning, "ingest", lambda s, ws, m, rows: {"accepted": len(rows), "duplicate": 0})
    monkeypatch.setattr(learning, "totals", lambda s, ws: {"rework": 1})
    monkeypatch.setattr(learning, "lessons", lambda s, ws: [])
    monkeypatch.setattr(learning, "propose", lambda s, ws, m, t: {"id": "l_1", "status": "held", "proposed_by": m})
    c, _ = client("member")
    assert c.post("/api/learn/rows", json={"rows": [{}]}).json() == {"accepted": 1, "duplicate": 0}
    assert c.get("/api/learn/totals").json() == {"rework": 1}
    assert c.get("/api/lessons").json() == []
    assert c.post("/api/lessons", json={"text": "t"}).json()["status"] == "held"


def test_webhook_secret_not_echoed():
    c, store = client()
    r = c.post("/api/webhooks", json={"url": "http://x/h", "secret": "topsecret"})
    assert r.status_code == 200 and "topsecret" not in r.text
    assert store.query("SELECT url FROM webhooks") == [{"url": "http://x/h"}]


def test_sign():
    import hashlib, hmac
    assert sign("s", b"{}") == "sha256=" + hmac.new(b"s", b"{}", hashlib.sha256).hexdigest()


def test_notifier_signature():
    store = Store(":memory:")
    store.exec("INSERT INTO webhooks(id,workspace,url,secret) VALUES(?,?,?,?)", ("h", "w1", "http://x/h", "s"))
    calls = []
    Notifier(store, post=lambda url, **kw: calls.append((url, kw)))(FakeTask())
    url, kw = calls[0]
    assert url == "http://x/h"
    assert kw["headers"]["X-Slcloud-Signature"] == sign("s", kw["content"])
    assert json.loads(kw["content"])["task"]["id"] == PUB["id"]


def test_notifier_post_failure_swallowed():
    store = Store(":memory:")
    for i in range(2):
        store.exec("INSERT INTO webhooks(id,workspace,url,secret) VALUES(?,?,?,?)", (f"h{i}", "w1", f"http://x/{i}", "s"))
    n = []

    def post(url, **kw):
        n.append(url)
        raise RuntimeError("down")
    Notifier(store, post=post)(FakeTask())
    assert len(n) == 2
