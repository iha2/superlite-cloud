import os
from types import SimpleNamespace

import pytest

from slcloud import mcp_server
from slcloud.errors import BadTransition, NotFound

ID = "t_0123456789ab"


def pub(status="queued", result=None):
    return {"id": ID, "repo": "r", "prompt": "p", "agent": "claude", "base": "HEAD", "status": status,
            "plan": None, "result": result, "error": None, "created": "x", "updated": "x"}


class FakeTask:
    def __init__(self, d):
        self.d = d

    def public(self):
        return self.d


class FakeEngine:
    def __init__(self, status="queued", result=None):
        self.status, self.result = status, result
        self.calls = []

    def _check(self, ws, tid):
        if ws != "w1" or tid != ID:
            raise NotFound(tid)

    def submit(self, ws, repo, prompt, agent, base):
        self.calls.append(("submit", ws, repo, prompt, agent, base))
        return FakeTask(pub())

    def get(self, ws, tid):
        self._check(ws, tid)
        return FakeTask(pub(self.status, self.result))

    def list(self, ws):
        return [FakeTask(pub())] if ws == "w1" else []

    def respond(self, ws, tid, answer):
        self._check(ws, tid)
        if self.status != "waiting":
            raise BadTransition("not waiting")
        return FakeTask(pub("queued"))

    def cancel(self, ws, tid):
        self._check(ws, tid)
        return FakeTask(pub("cancelled"))


@pytest.fixture
def make(monkeypatch):
    from slcloud import learning

    if not os.environ.get("SUPERLITE_REAL"):
        monkeypatch.setattr(learning, "lessons", lambda store, ws: [
            {"id": "l_1a2b3c4d", "text": "run lint first", "proposed_by": "m1", "approved_by": "m2"}] if ws == "w1" else [])

    def build(ws="w1", **kw):
        who = lambda: SimpleNamespace(workspace=ws, role="member", member="m1")
        settings = SimpleNamespace(public_url="http://h:8787")
        return mcp_server.tools(FakeEngine(**kw), None, settings, who)

    return build


def test_tool_names(make):
    assert set(make()) == {"submit_task", "get_task", "list_tasks", "respond", "cancel_task",
                           "fetch_command", "team_lessons"}


def test_build_mcp_registers_tools():
    mcp = mcp_server.build_mcp(FakeEngine(), None, SimpleNamespace(public_url="http://h"))
    assert mcp.name == "slcloud"


def test_submit(make):
    assert make()["submit_task"](repo="r", prompt="p") == {"task_id": ID, "status": "queued"}


def test_get_and_list(make):
    t = make()
    assert t["get_task"](task_id=ID)["id"] == ID
    assert [x["id"] for x in t["list_tasks"]()] == [ID]


def test_other_workspace(make):
    assert make(ws="w2")["get_task"](task_id=ID) == {"error": "not found"}


def test_respond_and_cancel(make):
    assert make(status="waiting")["respond"](task_id=ID, answer="approve")["status"] == "queued"
    assert make()["respond"](task_id=ID, answer="approve")["error"] == "not waiting"
    assert make()["cancel_task"](task_id=ID)["status"] == "cancelled"


def test_fetch_bundle(make):
    res = {"branch": "slcloud/t_1", "sha": "9c", "pushed": False, "bundle": "/out/t_1.bundle",
           "fetch": "git fetch <bundle-url> slcloud/t_1"}
    out = make(status="done", result=res)["fetch_command"](task_id=ID)
    assert out["bundle_url"] == f"http://h:8787/api/tasks/{ID}/bundle"
    assert out["command"] == f"git fetch http://h:8787/api/tasks/{ID}/bundle slcloud/t_1"


def test_fetch_pushed(make):
    res = {"branch": "b", "sha": "9c", "pushed": True, "bundle": None, "fetch": "git fetch origin b"}
    assert make(status="done", result=res)["fetch_command"](task_id=ID) == {"command": "git fetch origin b"}


def test_fetch_not_done(make):
    assert make()["fetch_command"](task_id=ID) == {"error": "task is not done"}


def test_team_lessons(make):
    assert make()["team_lessons"]()[0]["text"] == "run lint first"
    assert make(ws="w2")["team_lessons"]() == []


def test_listed_schema_has_real_params():
    import asyncio

    mcp = mcp_server.build_mcp(FakeEngine(), None, SimpleNamespace(public_url="http://h"))
    listed = asyncio.run(mcp.list_tools())
    schema = {t.name: t.inputSchema for t in listed}["submit_task"]
    assert {"repo", "prompt"} <= set(schema["properties"])
    assert "kw" not in schema["properties"]
    assert "kw" not in schema.get("required", [])
