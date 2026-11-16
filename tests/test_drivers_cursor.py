import pytest

from slcloud.drivers import get_driver
from slcloud.drivers.cursor import CursorDriver


class FakeProc:
    def __init__(self, out):
        self.out, self.written = out, []

    def write(self, text):
        self.written.append(text)

    def lines(self):
        return iter(self.out)

    def close_stdin(self):
        pass

    def wait(self):
        return None

    def kill(self):
        pass


def make(turns, session=None):
    argvs, it = [], iter(turns)

    def start(argv):
        argvs.append(argv)
        return FakeProc(next(it))

    return CursorDriver(start, session), argvs


T1 = ['{"type":"system","session_id":"c_1"}', '{"type":"result","result":"one","session_id":"c_1"}']
T2 = ['{"type":"result","result":"two","session_id":"c_1"}']


def test_nudge_queued_and_registry():
    d, _ = make([T1])
    assert d.nudge("early") == "unsupported"
    d.start("go")
    assert d.nudge("use v2") == "queued"
    assert isinstance(get_driver("cursor", lambda a: None, session="c_1"), CursorDriver)


def test_cursor_same_chat():
    d, argvs = make([T1, T2])
    d.start("go")
    it = d.lines()
    next(it)
    d.nudge("use v2")
    assert len(argvs) == 1
    assert list(it) == T1[1:] + T2
    assert len(argvs) == 2
    a = argvs[1]
    assert a[a.index("--resume") + 1] == "c_1"
    assert a[-1] == "use v2"
    assert d.result().text == "two"
    assert "--resume" not in argvs[0]


def test_queued_in_order():
    d, argvs = make([T1, T2])
    d.start("go")
    d.nudge("first")
    d.nudge("second")
    list(d.lines())
    assert len(argvs) == 2
    p = argvs[1][-1]
    assert p.index("first") < p.index("second")


def test_stop_drops_queue():
    d, argvs = make([T1, T2])
    d.start("go")
    d.nudge("x")
    d.stop()
    list(d.lines())
    assert len(argvs) == 1
    assert d.nudge("y") == "unsupported"


def test_key_not_in_argv(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "key-abcdefghijklmnop1234")
    d, argvs = make([T1, T2])
    d.start("go")
    d.nudge("more")
    list(d.lines())
    assert not any("key-abcdef" in a for argv in argvs for a in argv)


def test_resume_session_given():
    d, argvs = make([T2], session="c_9")
    d.start("go")
    assert argvs[0][argvs[0].index("--resume") + 1] == "c_9"


def test_events():
    d, _ = make([])
    assert d.events('{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}') == [{"kind": "text", "text": "hi"}]
    tool = '{"type":"tool_call","subtype":"started","tool_call":{"editToolCall":{"args":{"path":"a.py"}}}}'
    assert d.events(tool) == [{"kind": "tool", "tool": "editToolCall", "target": "a.py"}]
    assert d.events("junk") == []


def test_intermediate_result_not_terminal_while_queued():
    d, _ = make([T1, T2])
    d.start("go")
    d.nudge("more")
    assert d.events(T1[1]) == []
    assert d.events('{"type":"result","result":"x","is_error":true}') == [{"kind": "status", "status": "error"}]
    list(d.lines())
    assert d.events(T2[0]) == [{"kind": "status", "status": "done"}]


def test_on_resume_reports_batch_size():
    d, _ = make([T1, T2])
    got = []
    d.on_resume = got.append
    d.start("go")
    d.nudge("a")
    d.nudge("b")
    list(d.lines())
    assert got == [2]
