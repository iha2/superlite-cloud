import json
import threading

import pytest

from slcloud.drivers.codex import CodexDriver
from slcloud.drivers.gemini import GeminiDriver


class FakeProc:
    """Answers each request via `reply(msg) -> list[dict|str]`; unlisted ones get {}."""

    def __init__(self, reply):
        self.sent, self.reply = [], reply
        self.q = __import__("queue").Queue()
        self.killed = False

    def write(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        for out in self.reply(msg):
            self.q.put(out if isinstance(out, str) else json.dumps(out))

    def lines(self):
        while True:
            x = self.q.get()
            if x is None:
                return
            yield x

    def close_stdin(self):
        pass

    def kill(self):
        self.killed = True
        self.q.put(None)

    def push(self, obj):
        self.q.put(obj if isinstance(obj, str) else json.dumps(obj))


def codex_reply(steer_error=False):
    def reply(m):
        r = {"jsonrpc": "2.0", "id": m.get("id")}
        if m.get("method") == "thread/start":
            return [{**r, "result": {"thread": {"id": "th_1"}}}]
        if m.get("method") == "turn/steer" and steer_error:
            return [{**r, "error": {"code": -32601, "message": "no"}}]
        return [{**r, "result": {}}] if "id" in m else []
    return reply


def mk(cls, reply):
    fp = FakeProc(reply)
    d = cls(lambda argv: fp)
    return d, fp


def test_codex_handshake():
    d, fp = mk(CodexDriver, codex_reply())
    d.start("fix it")
    methods = [m["method"] for m in fp.sent if "id" in m]
    assert methods == ["initialize", "thread/start", "turn/start"]
    assert fp.sent[-1]["params"] == {"threadId": "th_1",
                                     "input": [{"type": "text", "text": "fix it"}]}
    d.stop()


def test_codex_nudge_delivered():
    d, fp = mk(CodexDriver, codex_reply())
    d.start("x")
    assert d.nudge("use v2") == "delivered"
    s = fp.sent[-1]
    assert s["method"] == "turn/steer" and s["jsonrpc"] == "2.0" and isinstance(s["id"], int)
    assert s["params"] == {"threadId": "th_1", "input": [{"type": "text", "text": "use v2"}]}
    d.stop()


def test_rpc_error_watch_only():
    d, fp = mk(CodexDriver, codex_reply(steer_error=True))
    d.start("x")
    assert d.nudge("use v2") == "unsupported"
    d.stop()


def test_bad_json_line_skipped():
    d, fp = mk(CodexDriver, codex_reply())
    d.start("x")
    fp.push("not json {")
    fp.push({"jsonrpc": "2.0", "method": "turn/completed", "params": {}})
    got = list(d.lines())
    assert "not json {" in got
    assert [e for line in got for e in d.events(line)] == [{"kind": "done"}]


def test_codex_events_and_result():
    d, fp = mk(CodexDriver, codex_reply())
    d.start("x")
    ev = d.events(json.dumps({"method": "item/started", "params": {
        "item": {"type": "commandExecution", "command": "ls"}}}))
    assert ev == [{"kind": "tool", "tool": "Bash", "target": "ls"}]
    d.events(json.dumps({"method": "item/completed", "params": {
        "item": {"type": "agentMessage", "text": "done"}}}))
    r = d.result()
    assert r.text == "done" and r.session == "th_1" and r.ok
    d.stop()


def gem_reply(m):
    r = {"jsonrpc": "2.0", "id": m.get("id")}
    if m["method"] == "session/new":
        return [{**r, "result": {"sessionId": "s_1"}}]
    if m["method"] == "session/prompt":
        return []
    return [{**r, "result": {}}] if "id" in m else []


def test_gemini_handshake():
    d, fp = mk(GeminiDriver, gem_reply)
    d.start("fix it")
    assert [m["method"] for m in fp.sent] == ["initialize", "session/new", "session/prompt"]
    assert fp.sent[-1]["params"]["sessionId"] == "s_1"
    d.stop()


def test_gemini_nudge_cancels_then_prompts():
    d, fp = mk(GeminiDriver, gem_reply)
    d.start("x")
    assert d.nudge("use v2") == "delivered"
    a, b = fp.sent[-2:]
    assert a["method"] == "session/cancel" and "id" not in a
    assert b["method"] == "session/prompt" and b["params"]["sessionId"] == "s_1"
    assert b["params"]["prompt"] == [{"type": "text", "text": "use v2"}]
    # the cancelled first prompt's response does not end the run; the last one does
    first = fp.sent[2]["id"]
    assert d.events(json.dumps({"jsonrpc": "2.0", "id": first,
                                "result": {"stopReason": "cancelled"}})) == []
    d.stop()


def test_gemini_bad_line_and_events():
    d, fp = mk(GeminiDriver, gem_reply)
    d.start("x")
    assert d.events("garbage") == []
    d.events(json.dumps({"method": "session/update", "params": {"update": {
        "sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}}}))
    assert d.result().text == "hi"
    fp.push({"jsonrpc": "2.0", "id": fp.sent[-1]["id"], "result": {"stopReason": "end_turn"}})
    got = list(d.lines())
    assert d.events(got[-1]) == [{"kind": "done"}]


def test_nudge_after_stop_unsupported():
    for cls, reply in ((CodexDriver, codex_reply()), (GeminiDriver, gem_reply)):
        d, fp = mk(cls, reply)
        d.start("x")
        d.stop()
        n = len(fp.sent)
        assert d.nudge("late") == "unsupported" and len(fp.sent) == n
