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

    def __getattr__(self, k):
        return self.d[k]


class FakeEngine:
    def __init__(self, status="queued", result=None):
        self.status, self.result = status, result
        self.calls = []

    def submit(self, ws, repo, prompt, agent, base, *, owner, name=None, orchestrator="ai"):
        self.calls.append(("submit", ws, repo, prompt, agent, base, owner))
        self.orchestrators = getattr(self, "orchestrators", []) + [orchestrator]
        return FakeTask({**pub(), "orchestrator": orchestrator})

    def list(self, ws, owner=None):
        return [FakeTask(pub())] if ws == "w1" else []

    def respond(self, ws, tid, answer):
        if self.status != "waiting":
            raise BadTransition("not waiting")
        return FakeTask(pub("queued"))

    def cancel(self, ws, tid):
        return FakeTask(pub("cancelled"))


@pytest.fixture
def make(monkeypatch):
    from slcloud import learning, runs
    from slcloud.store import Store

    store = Store(":memory:")
    prop = learning.propose(store, "w1", "m1", "run lint first")
    learning.approve(store, "w1", prop["id"], "m2")
    seen = []

    def build(ws="w1", **kw):
        eng = FakeEngine(**kw)

        def get_run(store, who, ref):
            if who.workspace != "w1" or ref not in (ID, "login_fix"):
                raise NotFound(ref)
            return FakeTask(pub(eng.status, eng.result))

        monkeypatch.setattr(runs, "get_run", get_run)
        def give(st, e, who, ref, email):
            get_run(st, who, ref)
            return FakeTask({**pub(), "owner": "m_1a2b3c4d", "name": "login_fix_2", "email": email})

        monkeypatch.setattr(runs, "give", give)
        monkeypatch.setattr(runs, "notices", lambda st, m: [{"id": 1, "task": "t_1", "text": "given", "seen": 0}])
        monkeypatch.setattr(runs, "mark_seen", lambda st, m: seen.append(m))
        who = lambda: SimpleNamespace(workspace=ws, role="member", member="m1")
        settings = SimpleNamespace(public_url="http://h:8787")
        out = mcp_server.tools(eng, store, settings, who)
        out["_eng"], out["_seen"] = eng, seen
        return out

    return build


def test_tool_names(make):
    assert {k for k in make() if not k.startswith("_")} == {
        "submit_task", "get_task", "list_tasks", "respond", "cancel_task", "fetch_command",
        "team_lessons", "list_runs", "give_run", "status",
        "list_files", "read_file", "write_file"}


def test_give_run_status_list_runs(make):
    t = make()
    assert t["give_run"](run="login_fix", email="pat@example.com")["name"] == "login_fix_2"
    assert t["status"]()["notices"][0]["task"] == "t_1"
    assert [x["id"] for x in t["list_runs"]()] == [ID] and t["_seen"] == ["m1"]
    assert make(ws="w2")["give_run"](run="login_fix", email="pat@example.com") == {"error": "not found"}


def test_give_run_unknown_member(make, monkeypatch):
    from slcloud import runs

    def give(*a, **kw):
        raise runs.NoSuchMember("no such member")

    t = make()
    before = t["list_runs"]()
    monkeypatch.setattr(runs, "give", give)
    assert t["give_run"](run="login_fix", email="nobody@example.com") == {"error": "no such member"}
    assert t["list_runs"]() == before


def test_build_mcp_registers_tools():
    mcp = mcp_server.build_mcp(FakeEngine(), None, SimpleNamespace(public_url="http://h"))
    assert mcp.name == "slcloud"


def test_submit(make):
    assert make()["submit_task"](repo="r", prompt="p") == {"task_id": ID, "status": "queued"}
    t = make()
    t["submit_task"](repo="r", prompt="p")
    assert t["_eng"].calls[0][-1] == "m1"


def test_mcp_submit_human(make):
    t = make()
    out = t["submit_task"](repo="r", prompt="fix the login bug", orchestrator="human")
    assert out == {"task_id": ID, "status": "queued"}
    t["submit_task"](repo="r", prompt="p")
    assert t["_eng"].orchestrators == ["human", "ai"]


def test_mcp_submit_bad_orchestrator(make):
    t = make()
    t["_eng"].submit = lambda *a, **k: (_ for _ in ()).throw(ValueError("orchestrator must be ai or human"))
    assert t["submit_task"](repo="r", prompt="p", orchestrator="robot") == {
        "error": "orchestrator must be ai or human"}


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
    out = make()["team_lessons"]()
    assert out[0]["text"] == "run lint first" and out[0]["proposed_by"] == "m1" and out[0]["approved_by"] == "m2"
    assert make(ws="w2")["team_lessons"]() == []


def test_listed_schema_has_real_params():
    import asyncio

    mcp = mcp_server.build_mcp(FakeEngine(), None, SimpleNamespace(public_url="http://h"))
    listed = asyncio.run(mcp.list_tools())
    schema = {t.name: t.inputSchema for t in listed}["submit_task"]
    assert {"repo", "prompt"} <= set(schema["properties"])
    assert "kw" not in schema["properties"]
    assert "kw" not in schema.get("required", [])
