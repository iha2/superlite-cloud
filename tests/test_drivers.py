import json

import pytest

from slcloud.adapters import PLAN_MARKER
from slcloud.drivers import get_driver
from slcloud.drivers.claude import ClaudeDriver


class FakeProc:
    def __init__(self, out=()):
        self.out, self.written, self.closed = list(out), [], False

    def write(self, text):
        self.written.append(text)

    def lines(self):
        return iter(self.out)

    def close_stdin(self):
        self.closed = True

    def wait(self):
        return None

    def kill(self):
        pass


def make(out=(), session=None):
    proc, argvs = FakeProc(out), []

    def start(argv):
        argvs.append(argv)
        return proc

    return get_driver("claude", start, session), proc, argvs


def test_claude_stream_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretsecretsecret1")
    d, proc, argvs = make()
    d.start("fix it")
    argv = argvs[0]
    assert argv[0] == "claude"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert not any("sk-ant" in a for a in argv)
    assert "fix it" not in " ".join(argv)
    assert json.loads(proc.written[0])["message"]["content"] == "fix it"


@pytest.mark.parametrize("session", [None, "s_9"])
def test_claude_driver_isolation(session):
    """Regression: the live path (this driver, not ClaudeAdapter) still passed --bare and acceptEdits, so
    superlite never loaded in real runs. Same isolation flags as the adapter, never the repo's config."""
    d, _, argvs = make(session=session)
    d.start("x")
    argv = argvs[0]
    assert "--bare" not in argv and "acceptEdits" not in argv
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    assert argv[argv.index("--plugin-dir") + 1] == "/opt/superlite"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_claude_driver_stays_in_work():
    """Regression: a live superlite run built in /work/.claude/worktrees/<x>, and the hand-back (which commits
    /work only) came back empty. Worktree tools are disallowed and the note names the worker's session."""
    d = get_driver("claude", lambda a: FakeProc(), None, slug="sl-headless")
    argv = d.argv("x")
    i = argv.index("--disallowedTools")
    assert argv[i + 1:i + 3] == ["EnterWorktree", "ExitWorktree"]
    note = argv[argv.index("--append-system-prompt") + 1]
    assert "`sl-headless`" in note and "/work" in note and "worktree" in note
    assert "--slug sl-headless" in note
    assert "None" not in get_driver("claude", lambda a: FakeProc()).argv("x")[-1]


def test_claude_driver_variadic_flag_order():
    """--disallowedTools is variadic: a flag must follow its two tool names, --resume must not be swallowed,
    and the prompt goes over stdin so no positional trails the argv."""
    argv = get_driver("claude", lambda a: FakeProc(), "s_9", slug="x").argv("p")
    i = argv.index("--disallowedTools")
    assert argv[i + 3].startswith("--")
    assert argv.index("--resume") > i + 3
    assert argv[-2:] == ["--resume", "s_9"] and "p" not in argv


def test_adapter_env_cannot_override_key():
    from slcloud.adapters import ClaudeAdapter
    assert ClaudeAdapter.key_env not in ClaudeAdapter.env
    assert {**ClaudeAdapter.env, ClaudeAdapter.key_env: "k"}[ClaudeAdapter.key_env] == "k"


def test_get_driver_claude_and_resume():
    d, _, argvs = make(session="s_9")
    assert isinstance(d, ClaudeDriver) and d.session == "s_9"
    d.start("x")
    assert argvs[0][-2:] == ["--resume", "s_9"]


def test_unknown_agent():
    with pytest.raises(ValueError):
        get_driver("x", lambda a: FakeProc())


def test_nudge_delivered_and_after_stop():
    d, proc, _ = make()
    d.start("go")
    assert d.nudge("use v2") == "delivered"
    assert proc.written[-1] == '{"type":"user","message":{"role":"user","content":"use v2"}}\n'
    n = len(proc.written)
    d.stop()
    assert proc.closed
    assert d.nudge("late") == "unsupported"
    assert len(proc.written) == n


def test_nudge_before_start():
    d, _, _ = make()
    assert d.nudge("x") == "unsupported"


def test_events():
    d, _, _ = make()
    tool = '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit","input":{"file_path":"api.py"}}]}}'
    assert d.events(tool) == [{"kind": "tool", "tool": "Edit", "target": "api.py"}]
    text = '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}'
    assert d.events(text) == [{"kind": "text", "text": "hi"}]
    assert d.events("not json") == []
    assert d.events('{"type":"result","result":"x"}') == [{"kind": "status", "status": "done"}]


def test_events_thinking_blocks():
    d, _, _ = make()
    line = ('{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"hmm"},'
            '{"type":"text","text":"ok"},{"type":"thinking","thinking":""},{"type":"thinking"},'
            '{"type":"thinking","thinking":"more"}]}}')
    assert d.events(line) == [{"kind": "thinking", "text": "hmm"}, {"kind": "text", "text": "ok"},
                              {"kind": "thinking", "text": "more"}]


def test_result_and_lines():
    out = ['{"type":"system","session_id":"s_1"}', '{"type":"result","result":"done","session_id":"s_1","is_error":false}']
    d, _, _ = make(out)
    d.start("go")
    assert list(d.lines()) == out
    r = d.result()
    assert (r.text, r.plan, r.session, r.ok) == ("done", None, "s_1", True)


def test_result_plan():
    out = [json.dumps({"type": "result", "result": f"1. step\n{PLAN_MARKER}", "session_id": "s_2"})]
    d, _, _ = make(out)
    d.start("plan")
    list(d.lines())
    assert d.result().plan and PLAN_MARKER in d.result().plan


def test_result_without_output():
    d, _, _ = make()
    d.start("x")
    list(d.lines())
    assert d.result().ok is False


@pytest.mark.parametrize("agent", ["cursor", "codex", "gemini"])
def test_other_agents_resolve(agent):
    try:
        d = get_driver(agent, lambda a: FakeProc(), session="c_1")
    except (ImportError, NotImplementedError):
        pytest.skip("owned by T20/T8")
    assert d.session == "c_1"


def test_claude_driver_note_uses_given_workdir():
    note = get_driver("claude", lambda a: FakeProc(), None, slug="s", workdir="/data/work/t1").argv("x")[-1]
    assert "open in /data/work/t1 on its branch" in note and "/work" not in note.replace("/data/work", "")
    default = get_driver("claude", lambda a: FakeProc(), None, slug="s").argv("x")
    assert "open in /work on its branch" in default[default.index("--append-system-prompt") + 1]


def _agent_fixture():
    from pathlib import Path
    p = Path(__file__).parent / "fixtures" / "claude_stream_agents.jsonl"
    return [x for x in p.read_text().splitlines() if x.strip()]


def test_claude_agent_events_from_fixture():
    d, _, _ = make()
    ev = [e for ln in _agent_fixture() for e in d.events(ln)]
    ag = [e for e in ev if e["kind"] == "agent"]
    a, b = "toolu_01CCzTk6ChrKFXqbJGzyq8AH", "toolu_013vSWEaVAkM9DRxP7amjwkR"
    assert [e["id"] for e in ag if e["state"] == "start"] == [a, b]
    ends = {e["id"]: e for e in ag if e["state"] == "done"}
    assert ends[a]["tokens"] == 17875 and ends[b]["tokens"] == 16341


def test_tracker_per_driver():
    d1, _, _ = make()
    d2, _, _ = make()
    line = json.dumps({"type": "system", "subtype": "task_started", "tool_use_id": "toolu_X"})
    assert [e["state"] for e in d1.events(line)] == ["start"]
    assert [e["state"] for e in d1.events(line)] == []
    assert [e["state"] for e in d2.events(line)] == ["start"]


def test_events_non_assistant_lines_still_parse():
    d, _, _ = make()
    out = d.events(json.dumps({"type": "system", "subtype": "task_started", "tool_use_id": "toolu_Y"}))
    assert [e["kind"] for e in out] == ["agent"]
    assert d.events("[1]") == [] and d.events("null") == []


def test_claude_result_and_assistant_lines_unchanged_with_tracker():
    d, _, _ = make()
    assert d.events(json.dumps({"type": "result", "is_error": True, "result": "x"})) == [{"kind": "status", "status": "error"}]
    a = {"type": "assistant", "message": {"id": "m", "content": [{"type": "text", "text": "hi"}]}}
    assert d.events(json.dumps(a)) == [{"kind": "text", "text": "hi"}]
    d.events(json.dumps({"type": "system", "subtype": "task_started", "tool_use_id": "toolu_Z"}))
    mixed = {"type": "assistant", "message": {"content": [{"type": "text", "text": "t"}]}, "parent_tool_use_id": "toolu_Z"}
    assert [e["kind"] for e in d.events(json.dumps(mixed))] == ["text"]
