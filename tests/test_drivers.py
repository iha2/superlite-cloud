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
