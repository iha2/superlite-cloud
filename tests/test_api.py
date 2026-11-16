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
        self.d = {**PUB, "name": "login_fix", "owner": "m1", **kw}

    def __getattr__(self, k):
        try:
            return self.d[k]
        except KeyError:
            raise AttributeError(k) from None

    def public(self):
        return self.d


class FakeEngine:
    def __init__(self):
        self.submitted = []
        self.listed = []

    def submit(self, ws, repo, prompt, agent, base, *, owner, name=None):
        self.submitted.append({"owner": owner, "name": name})
        return FakeTask(repo=repo, prompt=prompt, agent=agent, base=base, owner=owner)

    def list(self, ws, owner=None):
        self.listed.append(owner)
        return [FakeTask()]

    def respond(self, ws, tid, answer):
        raise BadTransition("not waiting")

    def cancel(self, ws, tid):
        return FakeTask(status="cancelled")

    def events(self, tid, after=0):
        return [{"id": 1, "kind": "status", "data": {"to": "queued"}, "at": "t"}]


class FakeRuns:
    """Stands in for slcloud.runs (T10): only the owner or an admin sees a run."""

    def __init__(self):
        self.seen = []
        self.noted = [{"id": 1, "task": "t_1", "text": "given by chris to pat (pat@example.com)", "seen": 0}]

    def get_run(self, store, who, ref):
        if ref not in ("login_fix", "chris/login_fix", PUB["id"]) or (who.role != "admin" and who.member != "m1"):
            raise NotFound(ref)
        return FakeTask()

    def give(self, store, engine, who, ref, email):
        self.get_run(store, who, ref)
        return FakeTask(owner="m_1a2b3c4d", name="login_fix_2")

    def notices(self, store, member):
        return [] if member in self.seen else self.noted

    def mark_seen(self, store, member):
        self.seen.append(member)


@pytest.fixture(autouse=True)
def fake_runs(monkeypatch):
    from slcloud import runs
    f = FakeRuns()
    for n in ("get_run", "give", "notices", "mark_seen"):
        monkeypatch.setattr(runs, n, getattr(f, n), raising=False)
    return f


def client(role="admin", store=None, member="m1", engine=None):
    store = store or Store(":memory:")
    s = Settings("/d", "/d/x.db", "/d/work", "k")
    who = lambda: SimpleNamespace(workspace="w1", role=role, member=member)
    return TestClient(create_app(s, store, engine or FakeEngine(), who)), store


def test_health_and_task_routes():
    c, _ = client()
    assert c.get("/api/health").json() == {"ok": True}
    r = c.post("/api/tasks", json={"repo": "r", "prompt": "p"})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert c.get("/api/tasks").json()[0]["id"] == PUB["id"]
    assert c.get(f"/api/tasks/{PUB['id']}").json()["id"] == PUB["id"]
    assert c.get("/api/tasks/chris/login_fix").json()["name"] == "login_fix"
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


def test_every_route_non_owner_404():
    c, _ = client("member", member="m_other")
    gone = {"error": "not found"}
    calls = [
        c.get("/api/tasks/login_fix"),
        c.get("/api/tasks/login_fix/events"),
        c.post("/api/tasks/login_fix/respond", json={"answer": "approve"}),
        c.post("/api/tasks/login_fix/cancel"),
        c.get("/api/tasks/login_fix/bundle"),
        c.post("/api/runs/login_fix/give", json={"email": "pat@example.com"}),
        c.get("/api/runs/login_fix/attach"),
        c.get("/api/runs/login_fix/events"),
        c.post("/api/runs/login_fix/nudge", json={"text": "x"}),
    ]
    for r in calls:
        assert r.status_code == 404 and r.json() == gone
    eng = FakeEngine()
    c, _ = client("member", member="m_other", engine=eng)
    assert c.get("/api/tasks").status_code == 200 and eng.listed == ["m_other"]


def test_give_by_email():
    c, _ = client("member")
    r = c.post("/api/runs/login_fix/give", json={"email": "PAT@example.com"})
    assert r.json()["owner"] == "m_1a2b3c4d" and r.json()["name"] == "login_fix_2"
    assert c.post("/api/runs/login_fix/give", json={}).status_code == 400


def test_give_response_has_recipient_handle_and_email():
    from slcloud import members
    c, store = client("member")
    members.create_member(store, "w1", "pat", "pat@example.com", "member")
    r = c.post("/api/runs/login_fix/give", json={"email": "PAT@example.com"})
    assert r.json()["to_handle"] == "pat" and r.json()["to_email"] == "pat@example.com"


def test_give_unknown_member_404(monkeypatch):
    from slcloud import runs
    owners = []

    def give(store, engine, who, ref, email):
        raise runs.NoSuchMember("no such member")

    monkeypatch.setattr(runs, "give", give, raising=False)
    c, _ = client("member")
    r = c.post("/api/runs/login_fix/give", json={"email": "nobody@example.com"})
    assert r.status_code == 404 and r.json() == {"error": "no such member"}
    assert c.get("/api/tasks/login_fix").json()["owner"] == "m1" and not owners


def test_body_owner_ignored():
    eng = FakeEngine()
    c, _ = client("member", engine=eng)
    r = c.post("/api/tasks", json={"repo": "r", "prompt": "p", "owner": "m_other"})
    assert r.json()["owner"] == "m1" and eng.submitted[0]["owner"] == "m1"


def test_all_admin_only():
    c, _ = client("member")
    assert c.get("/api/tasks?all=1").status_code == 403
    eng = FakeEngine()
    c, _ = client("admin", engine=eng)
    assert c.get("/api/tasks?all=1").status_code == 200 and eng.listed == [None]


def test_status_marks(fake_runs):
    c, _ = client("member")
    assert c.get("/api/status").json()["notices"][0]["task"] == "t_1"
    c.get("/api/tasks")
    assert c.get("/api/status").json() == {"notices": [], "runs": []}


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


def test_me_route():
    c, st = client("member", member="m1")
    st.exec("INSERT INTO members(id,workspace,handle,email,role) VALUES('m1','w1','chris','chris@example.com','member')")
    st.exec("INSERT INTO workspaces(id,name,created) VALUES('w1','acme','c')")
    assert c.get("/api/me").json() == {"handle": "chris", "email": "chris@example.com",
                                       "role": "member", "workspace": "acme"}


def test_status_has_runs_and_keeps_notices(fake_runs):
    from slcloud.engine import Engine
    c, st = client("member", member="m1")
    eng = Engine(st)
    t = eng.submit("w1", "r", "Fix the login redirect", "claude", "main", owner="m1")
    eng.claim()
    eng.add_event(t.id, "tool", {"step": 14, "tool": "Edit", "target": "api.py"})
    body = c.get("/api/status").json()
    assert body["runs"] == [{"name": "login_fix", "status": "running", "step": 14,
                             "doing": "Edit api.py", "agent": "claude"}]
    assert body["notices"][0]["task"] == "t_1"
    assert fake_runs.seen == []  # status never marks notices seen
    eng.transition(t.id, "done")
    assert c.get("/api/status").json()["runs"] == []


def test_attach_marks_that_run_seen():
    c, st = client("member", member="m1")
    st.exec("INSERT INTO notices(member,task,text,seen) VALUES('m1',?,'a',0)", (PUB["id"],))
    st.exec("INSERT INTO notices(member,task,text,seen) VALUES('m1','t_other','b',0)")
    r = c.get("/api/runs/login_fix/attach")
    assert r.json() == {"run": "login_fix", "id": PUB["id"], "status": "queued"}
    assert [x["seen"] for x in st.query("SELECT seen FROM notices ORDER BY id")] == [1, 0]
