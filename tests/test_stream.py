from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from slcloud import stream
from slcloud.errors import NotFound

EV = {"id": 418, "kind": "tool", "data": {"step": 14, "tool": "Edit", "target": "api.py"},
      "at": "2026-11-15T14:20:01Z"}
OLD = {"id": 417, "kind": "tool", "data": {"step": 13}, "at": "x"}


class FakeEngine:
    def __init__(self):
        self.nudges = []

    def events(self, task_id, after=0):
        return [e for e in (OLD, EV) if e["id"] > after]

    def add_nudge(self, task_id, sender, text):
        self.nudges.append((task_id, sender, text))
        return 3


def lives(n):
    c = {"n": n}

    def alive():
        c["n"] -= 1
        return c["n"] >= 0
    return alive


def test_first_frame_shape():
    out = list(stream.sse_lines(FakeEngine(), "t_1", 417, lives(2), lambda s: None, run="login_fix"))
    assert out[0] == ('id: 418\nevent: tool\ndata: {"run":"login_fix","step":14,'
                      '"tool":"Edit","target":"api.py"}\n\n')


def test_replay_after_last_event_id():
    out = list(stream.sse_lines(FakeEngine(), "t_1", 417, lives(3), lambda s: None))
    assert len(out) == 1 and out[0].startswith("id: 418")
    assert not any("id: 417" in o for o in out)


def test_keepalive():
    t = {"v": 0.0}

    def sleep(s):
        t["v"] += 10

    out = list(stream.sse_lines(FakeEngine(), "t_1", 418, lives(8), sleep, clock=lambda: t["v"]))
    assert ": keep-alive\n\n" in out


def test_stream_ends_when_hidden():
    out = list(stream.sse_lines(FakeEngine(), "t_1", 0, lambda: False, lambda s: None))
    assert out == []


def client(monkeypatch, hidden=False):
    eng = FakeEngine()

    def get_run(store, who, ref):
        if hidden:
            raise NotFound(ref)
        return SimpleNamespace(id="t_1", name="login_fix")

    monkeypatch.setattr(stream, "_get_run", get_run)
    # stop the route's live loop after one pass
    monkeypatch.setattr(stream.time, "sleep", lambda s: None)
    app = FastAPI()
    who = SimpleNamespace(workspace="w", role="member", member="m_9f2c1a7e")
    app.include_router(stream.make_router(None, eng, lambda: who))
    return TestClient(app), eng


def test_nudge_route(monkeypatch):
    c, eng = client(monkeypatch)
    r = c.post("/api/runs/login_fix/nudge", json={"text": "x"})
    assert r.status_code == 202 and r.json() == {"id": 3, "status": "queued"}
    assert eng.nudges == [("t_1", "m_9f2c1a7e", "x")]


def test_non_owner_404(monkeypatch):
    c, _ = client(monkeypatch, hidden=True)
    for r in (c.post("/api/runs/login_fix/nudge", json={"text": "x"}),
              c.get("/api/runs/login_fix/events")):
        assert r.status_code == 404 and r.json() == {"error": "not found"}
