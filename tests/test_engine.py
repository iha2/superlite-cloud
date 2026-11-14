import pytest

from slcloud.engine import Engine
from slcloud.errors import BadTransition, NotFound
from slcloud.store import Store


@pytest.fixture
def seen():
    return []


@pytest.fixture
def eng(seen):
    return Engine(Store(":memory:"), notify=lambda task: seen.append((task.id, task.status)))


def sub(eng, ws="w1", prompt="add x"):
    return eng.submit(ws, "https://g/r.git", prompt, "claude", "HEAD")


def test_submit_public(eng):
    t = sub(eng)
    d = t.public()
    assert t.id.startswith("t_") and len(t.id) == 14
    assert set(d) == {"id", "repo", "prompt", "agent", "base", "status", "plan",
                      "result", "error", "created", "updated"}
    assert {k: d[k] for k in ("repo", "prompt", "agent", "base", "status", "plan", "result", "error")} == {
        "repo": "https://g/r.git", "prompt": "add x", "agent": "claude", "base": "HEAD",
        "status": "queued", "plan": None, "result": None, "error": None}
    assert eng.events(t.id)[0]["kind"] == "status"
    assert eng.events(t.id)[0]["data"] == {"to": "queued"}
    assert "at" in eng.events(t.id)[0]


def test_unknown_agent(eng):
    with pytest.raises(ValueError):
        eng.submit("w1", "r", "p", "nope", "HEAD")


def test_bad_transition(eng):
    t = sub(eng)
    eng.claim()
    eng.transition(t.id, "done", result={"branch": "b"})
    with pytest.raises(BadTransition):
        eng.transition(t.id, "running")
    with pytest.raises(BadTransition):
        eng.transition(t.id, "queued")


def test_cross_workspace(eng):
    t = sub(eng)
    assert eng.get("w1", t.id).id == t.id
    with pytest.raises(NotFound):
        eng.get("w2", t.id)
    assert eng.list("w2") == []
    with pytest.raises(NotFound):
        eng.cancel("w2", t.id)


def test_list_newest_first(eng):
    a, b = sub(eng, prompt="a"), sub(eng, prompt="b")
    assert [t.id for t in eng.list("w1")] == [b.id, a.id]


def test_claim_oldest_once(eng):
    a, b = sub(eng, prompt="a"), sub(eng, prompt="b")
    c1, c2 = eng.claim(), eng.claim()
    assert (c1.id, c2.id) == (a.id, b.id)
    assert c1.status == "running"
    assert eng.claim() is None


def test_transition_waiting_and_respond(eng):
    t = sub(eng)
    eng.claim()
    w = eng.transition(t.id, "waiting", session="s1", plan="1. add x")
    assert (w.status, w.session, w.plan) == ("waiting", "s1", "1. add x")
    r = eng.respond("w1", t.id, "approve")
    assert (r.status, r.answer, r.session) == ("queued", "approve", "s1")
    d = r.public()
    assert "answer" not in d and "session" not in d and "ws" not in d and "workspace" not in d


def test_respond_needs_waiting(eng):
    t = sub(eng)
    with pytest.raises(BadTransition):
        eng.respond("w1", t.id, "approve")


def test_cancel_running(eng):
    t = sub(eng)
    eng.claim()
    assert eng.cancel("w1", t.id).status == "cancelled"


def test_cancel_done(eng):
    t = sub(eng)
    eng.claim()
    eng.transition(t.id, "done")
    with pytest.raises(BadTransition):
        eng.cancel("w1", t.id)


def test_result_json_and_error(eng):
    t = sub(eng)
    eng.claim()
    d = eng.transition(t.id, "done", result={"branch": "b", "sha": "abc"})
    assert d.public()["result"] == {"branch": "b", "sha": "abc"}
    assert eng.get("w1", t.id).public()["result"] == {"branch": "b", "sha": "abc"}
    u = sub(eng)
    eng.claim()
    assert eng.transition(u.id, "failed", error="boom").public()["error"] == "boom"


def test_notify_called(eng, seen):
    t = sub(eng)
    eng.claim()
    eng.transition(t.id, "waiting", session="s1", plan="p")
    eng.respond("w1", t.id, "approve")
    eng.cancel("w1", t.id)
    assert [s for _, s in seen] == ["queued", "running", "waiting", "queued", "cancelled"]
    assert [e["data"]["to"] for e in eng.events(t.id)] == [s for _, s in seen]


def test_unknown_field_and_task(eng):
    t = sub(eng)
    with pytest.raises(ValueError):
        eng.transition(t.id, "running", bogus=1)
    with pytest.raises(NotFound):
        eng.transition("t_000000000000", "running")


def test_notify_exception_keeps_state():
    def boom(task):
        raise RuntimeError("listener down")
    e = Engine(Store(":memory:"), notify=boom)
    t = sub(e)
    c = e.claim()
    assert c is not None and c.id == t.id and c.status == "running"
    assert e.transition(t.id, "done").status == "done"
    assert [x["data"]["to"] for x in e.events(t.id)] == ["queued", "running", "done"]


def test_concurrent_claim_atomic(eng):
    import threading
    for _ in range(5):
        sub(eng)
    got = []
    def w():
        for _ in range(5):
            c = eng.claim()
            if c:
                got.append(c.id)
    ths = [threading.Thread(target=w) for _ in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert len(got) == 5 and len(set(got)) == 5


def test_other_ws_respond_cancel(eng):
    t = sub(eng)
    with pytest.raises(NotFound):
        eng.cancel("w2", t.id)
    with pytest.raises(NotFound):
        eng.respond("w2", t.id, "x")
    assert eng.list("w2") == [] and eng.get("w1", t.id).status == "queued"


def test_add_event_appends(eng):
    t = sub(eng)
    eng.add_event(t.id, "notice", {"msg": "hi"})
    ev = eng.events(t.id)
    assert ev[-1]["kind"] == "notice" and ev[-1]["data"] == {"msg": "hi"} and ev[-1]["at"]
    assert eng.get("w1", t.id).status == "queued"


def test_add_event_unknown_task_notfound(eng):
    with pytest.raises(NotFound):
        eng.add_event("t_000000000000", "notice", {})
