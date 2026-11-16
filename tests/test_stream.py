import pytest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from slcloud import stream
from slcloud.store import Store
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
    store = Store(":memory:")
    store.exec("INSERT INTO notices(member,task,text,seen) VALUES('m_9f2c1a7e','t_1','a',0)")
    store.exec("INSERT INTO notices(member,task,text,seen) VALUES('m_9f2c1a7e','t_2','b',0)")
    app.include_router(stream.make_router(store, eng, lambda: who))
    c = TestClient(app)
    c.store = store
    return c, eng


def seen(c):
    return [r["seen"] for r in c.store.query("SELECT seen FROM notices ORDER BY id")]


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


def test_watch_marks_only_that_run_seen(monkeypatch):
    c, _ = client(monkeypatch)
    monkeypatch.setattr(stream, "sse_lines", lambda *a, **k: iter(()))
    assert c.get("/api/runs/login_fix/events").status_code == 200
    assert seen(c) == [1, 0]


def test_nudge_marks_only_that_run_seen(monkeypatch):
    c, _ = client(monkeypatch)
    assert c.post("/api/runs/login_fix/nudge", json={"text": "x"}).status_code == 202
    assert seen(c) == [1, 0]


def test_hidden_run_marks_nothing(monkeypatch):
    c, _ = client(monkeypatch, hidden=True)
    c.get("/api/runs/login_fix/events")
    c.post("/api/runs/login_fix/nudge", json={"text": "x"})
    assert seen(c) == [0, 0]


class ScriptEngine:
    def __init__(self, evs):
        self.evs = evs

    def events(self, task_id, after=0):
        return [e for e in self.evs if e["id"] > after]


def _ev(i, kind, data):
    return {"id": i, "kind": kind, "data": data, "at": "x"}


def test_stream_closes_after_terminal_status():
    evs = [_ev(1, "tool", {"step": 1}), _ev(2, "result", {"text": "ok"}),
           _ev(3, "status", {"to": "done"})]
    out = list(stream.sse_lines(ScriptEngine(evs), "t_1", 0, lambda: True, lambda s: None))
    assert [o.split("\n")[1] for o in out] == ["event: tool", "event: result", "event: status"]


@pytest.mark.parametrize("to", ["done", "failed", "cancelled"])
def test_each_terminal_status_closes(to):
    out = list(stream.sse_lines(ScriptEngine([_ev(1, "status", {"to": to})]), "t_1", 0,
                                lambda: True, lambda s: None))
    assert len(out) == 1


def test_waiting_status_does_not_close():
    out = list(stream.sse_lines(ScriptEngine([_ev(1, "status", {"to": "waiting"})]), "t_1", 0,
                                lives(3), lambda s: None))
    assert len(out) == 1  # ended by alive(), not by the stream closing itself


def test_replay_of_finished_run_sends_backlog_then_closes():
    evs = [_ev(1, "tool", {}), _ev(2, "status", {"to": "done"})]
    out = list(stream.sse_lines(ScriptEngine(evs), "t_1", 0, lambda: True, lambda s: None,
                                finished=lambda: True))
    assert len(out) == 2
    # Last-Event-ID past the end of a finished run: nothing to send, closes at once
    assert list(stream.sse_lines(ScriptEngine(evs), "t_1", 2, lambda: True, lambda s: None,
                                 finished=lambda: True)) == []


def test_events_route_closes_for_finished_run(monkeypatch):
    c, eng = client(monkeypatch)
    eng.events = lambda task_id, after=0: [e for e in (_ev(1, "status", {"to": "done"}),) if e["id"] > after]
    monkeypatch.setattr(stream, "_get_run",
                        lambda s, w, r: SimpleNamespace(id="t_1", name="login_fix", status="done"))
    r = c.get("/api/runs/login_fix/events")
    assert r.status_code == 200 and r.text.count("event: status") == 1
    r = c.get("/api/runs/login_fix/events", headers={"Last-Event-ID": "1"})
    assert r.status_code == 200 and r.text == ""


def test_nudge_ended_run_409(monkeypatch):
    for status in ("done", "failed", "cancelled"):
        c, eng = client(monkeypatch)
        monkeypatch.setattr(stream, "_get_run", lambda st, w, ref, s=status: SimpleNamespace(
            id="t_1", name="login_fix", status=s))
        r = c.post("/api/runs/login_fix/nudge", json={"text": "x"})
        assert r.status_code == 409 and r.json() == {"error": "login_fix has ended"}
        assert eng.nudges == []


def test_alive_and_finished_pinned_to_task_id(monkeypatch):
    refs = []

    def get_run(store, who, ref):
        refs.append(ref)
        return SimpleNamespace(id="t_1", name="login_fix", status="running")

    captured = {}

    def fake_sse(engine, task_id, after, alive, sleep, **k):
        captured["alive"], captured["finished"] = alive, k["finished"]
        return iter(())

    monkeypatch.setattr(stream, "_get_run", get_run)
    monkeypatch.setattr(stream, "sse_lines", fake_sse)
    app = FastAPI()
    who = SimpleNamespace(workspace="w", role="member", member="m_9f2c1a7e")
    app.include_router(stream.make_router(Store(":memory:"), FakeEngine(), lambda: who))
    assert TestClient(app).get("/api/runs/login_fix/events").status_code == 200
    refs.clear()
    assert captured["alive"]() is True
    captured["finished"]()
    assert refs == ["t_1", "t_1"]  # not the mutable name
