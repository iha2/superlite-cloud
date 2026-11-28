import json
from pathlib import Path

import pytest

from slcloud.drivers.agents import AgentTracker

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    return FIX

A = "toolu_01CCzTk6ChrKFXqbJGzyq8AH"
B = "toolu_013vSWEaVAkM9DRxP7amjwkR"


def load(fixtures_dir, name):
    return [json.loads(x) for x in (fixtures_dir / name).read_text().splitlines() if x.strip()]


def run(lines, **kw):
    t = AgentTracker(**kw)
    out = []
    for ln in lines:
        out += t.feed(ln)
    return out


def agent_line(i, parent=None, model="sonnet"):
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"id": "m" + i, "content": [
        {"type": "tool_use", "id": i, "name": "Agent", "input": {
            "description": "T1 · x", "subagent_type": "superlite:implementer", "model": model}}]}}


def started(i):
    return {"type": "system", "subtype": "task_started", "tool_use_id": i}


def test_sample_two_agents(fixtures_dir):
    ev = [e for e in run(load(fixtures_dir, "claude_stream_agents.jsonl")) if e["state"] != "update"]
    starts = [e for e in ev if e["state"] == "start"]
    assert [e["id"] for e in starts] == [A, B]
    assert all(e["parent"] is None and e["tier"] == "haiku" and e["type"] == "general-purpose" for e in starts)
    ends = {e["id"]: e for e in ev if e["state"] != "start"}
    assert ends[A]["state"] == "done" and ends[A]["tokens"] == 17875
    assert ends[B]["state"] == "done" and ends[B]["tokens"] == 16341
    assert len([e for e in ev if e["state"] != "start"]) == 2


def test_background_not_ended_by_async_launched(fixtures_dir):
    ev = run(load(fixtures_dir, "claude_stream_agents.jsonl")[:7])
    assert not [e for e in ev if e["id"] == B and e["state"] in ("done", "failed")]


def test_nested_parents(fixtures_dir):
    ev = run(load(fixtures_dir, "claude_stream_nested.jsonl"))
    starts = {e["id"]: e for e in ev if e["state"] == "start"}
    assert len(starts) == 8
    assert starts["toolu_N6"]["parent"] == "toolu_N4"
    assert starts["toolu_N7"]["parent"] == "toolu_N6"
    ends = [e for e in ev if e["state"] in ("done", "failed")]
    assert [e["id"] for e in ends if e["state"] == "failed"] == ["toolu_N3"]
    assert len([e for e in ends if e["state"] == "done"]) == 7


def test_stopped_is_failed():
    t = AgentTracker()
    assert t.feed(agent_line("toolu_A")) == []
    assert t.feed(started("toolu_A")) == [{
        "kind": "agent", "id": "toolu_A", "parent": None, "type": "implementer",
        "tier": "sonnet", "task": "T1 · x", "state": "start", "tokens": 0}]
    r = t.feed({"type": "system", "subtype": "task_notification", "tool_use_id": "toolu_A",
                "status": "stopped", "usage": {"total_tokens": 900}})
    assert r == [{"kind": "agent", "id": "toolu_A", "state": "failed", "tokens": 900}]


def sub(msg_id, parent, i=10, o=5):
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"id": msg_id, "model": "claude-opus-5", "usage": {"input_tokens": i, "output_tokens": o}}}


def test_update_throttle():
    now = [0.0]
    t = AgentTracker(clock=lambda: now[0])
    t.feed(agent_line("toolu_A", model=None))
    s = t.feed(started("toolu_A"))
    assert s[0]["tier"] == "?"
    u0 = t.feed(sub("m1", "toolu_A"))
    assert u0 == [{"kind": "agent", "id": "toolu_A", "state": "update", "tokens": 15}]
    now[0] = 1.0
    assert t.feed(sub("m2", "toolu_A")) == []
    assert t.feed(sub("m2", "toolu_A")) == []
    now[0] = 6.0
    u = t.feed(sub("m3", "toolu_A"))
    assert u == [{"kind": "agent", "id": "toolu_A", "state": "update", "tokens": 45}]
    assert t.feed(sub("m3", "toolu_A")) == []


def test_foreground_result_ends_once():
    t = AgentTracker()
    t.feed(agent_line("toolu_A"))
    t.feed(started("toolu_A"))
    res = {"type": "user", "parent_tool_use_id": None,
           "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_A"}]},
           "tool_use_result": {"status": "completed", "totalTokens": 77}}
    r = t.feed(res)
    assert r == [{"kind": "agent", "id": "toolu_A", "state": "done", "tokens": 77}]
    assert t.feed({"type": "system", "subtype": "task_notification", "tool_use_id": "toolu_A",
                   "status": "completed", "usage": {"total_tokens": 77}}) == []
    assert t.feed(res) == []


def test_garbage_lines():
    t = AgentTracker()
    for ln in ({}, {"type": "system"}, {"type": "assistant", "message": "x"},
               {"type": "user", "message": {"content": "x"}, "tool_use_result": "x"},
               None, "str", {"type": "system", "subtype": "task_started"},
               {"type": "assistant", "message": {"content": [None, 3]}}):
        assert t.feed(ln) == []


def _fixed_events():
    c = iter(float(n) for n in range(100000))
    return run(load(FIX, "claude_stream_agents.jsonl"), clock=lambda: next(c), gap=5.0)


def test_expected_events_fixture():
    assert json.loads((FIX / "agent_events.json").read_text()) == _fixed_events()
