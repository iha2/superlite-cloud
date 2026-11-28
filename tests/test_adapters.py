import json

import pytest

from slcloud.adapters import AgentResult, get_adapter

PLAN = "Plan ready. Reply approve to start"


def test_claude_command():
    assert get_adapter("claude").command("go", None) == [
        "claude", "-p", "--setting-sources", "user", "--strict-mcp-config",
        "--plugin-dir", "/opt/superlite",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions", "go"]
    c = get_adapter("claude").command("go", "s1")
    assert c[-3:] == ["--resume", "s1", "go"]


@pytest.mark.parametrize("resume", [None, "s1"])
def test_claude_command_isolation(resume):
    """Regression: superlite needs hooks and skills (no --bare, no acceptEdits), and the repo's own
    settings hooks and .mcp.json must never load (user settings only, strict MCP)."""
    c = get_adapter("claude").command("go", resume)
    assert "--bare" not in c and "acceptEdits" not in c
    assert c[c.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in c and "--mcp-config" not in c
    assert c[c.index("--permission-mode") + 1] == "bypassPermissions"
    assert get_adapter("claude").env == {"IS_SANDBOX": "1", "SUPERLITE_NO_AUTOCOMPACT": "1"}
    for other in ("codex", "gemini", "cursor"):
        assert get_adapter(other).env == {}


def test_claude_parse_plan():
    out = json.dumps({"type": "result", "result": PLAN, "session_id": "s1", "is_error": False})
    assert get_adapter("claude").parse(out) == AgentResult(PLAN, "s1", PLAN, True)


def test_claude_parse_stream_and_error():
    out = "\n".join([json.dumps({"type": "system", "session_id": "s1"}),
                     json.dumps({"type": "result", "result": "boom", "session_id": "s1", "is_error": True})])
    r = get_adapter("claude").parse(out)
    assert (r.text, r.session, r.plan, r.ok) == ("boom", "s1", None, False)


def test_codex_command():
    assert get_adapter("codex").command("go", "th1") == [
        "codex", "exec", "resume", "th1", "--json", "--full-auto", "go"]
    assert get_adapter("codex").command("go", None) == [
        "codex", "exec", "--json", "--full-auto", "--skip-git-repo-check", "go"]


def test_codex_parse():
    out = "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "th1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"type": "command_execution", "text": "ls"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": PLAN}},
    ])
    r = get_adapter("codex").parse(out)
    assert (r.text, r.session, r.plan, r.ok) == (PLAN, "th1", PLAN, True)


def test_gemini_json():
    a = get_adapter("gemini")
    assert a.command("go", None) == ["gemini", "-p", "go", "--output-format", "json", "--yolo"]
    r = a.parse('{"response": "ok"}')
    assert (r.text, r.session, r.plan, r.ok) == ("ok", None, None, True)


def test_cursor_json():
    a = get_adapter("cursor")
    assert a.command("go", None) == ["cursor-agent", "-p", "go", "--output-format", "json", "--force"]
    r = a.parse('{"result": "ok", "session_id": "c1"}')
    assert (r.text, r.session, r.ok) == ("ok", "c1", True)


@pytest.mark.parametrize("name", ["claude", "codex", "gemini", "cursor"])
def test_garbage_stdout(name):
    for junk in ["not json {", "", "[1,2]", "null", '{"x": 1}']:
        r = get_adapter(name).parse(junk)
        assert r.ok is False and r.plan is None


def test_unknown_agent():
    with pytest.raises(ValueError):
        get_adapter("nope")


def test_no_resume_support():
    with pytest.raises(ValueError):
        get_adapter("gemini").command("go", "s")
    with pytest.raises(ValueError):
        get_adapter("cursor").command("go", "s")


def test_attrs():
    table = {"claude": ("anthropic", "ANTHROPIC_API_KEY", True),
             "codex": ("openai", "OPENAI_API_KEY", True),
             "gemini": ("gemini", "GEMINI_API_KEY", False),
             "cursor": ("cursor", "CURSOR_API_KEY", False)}
    for n, (p, k, s) in table.items():
        a = get_adapter(n)
        assert (a.name, a.provider, a.key_env, a.supports_resume) == (n, p, k, s)


@pytest.mark.parametrize("name", ["claude", "codex", "gemini", "cursor"])
def test_dash_prompt_not_flag(name):
    c = get_adapter(name).command("--yolo rm", None)
    if name in ("gemini", "cursor"):
        assert c[c.index("-p") + 1] == " --yolo rm"
    else:
        assert c[-2:] == ["--", "--yolo rm"]


def test_bad_output_redacts_secrets():
    r = get_adapter("claude").parse("oops sk-abcdefghijklmnopqrstuvwx")
    assert "sk-abc" not in r.text and r.ok is False


def test_is_question():
    from slcloud.adapters import QUESTION_MARKER, is_question
    assert QUESTION_MARKER == "Reply to answer"
    assert is_question("Which login page: web or mobile?\nReply to answer")
    assert is_question("Which login page: web or mobile?\nReply to answer\n\n")
    assert not is_question("Reply to answer\nmore text after")
    assert not is_question("all done")
    assert not is_question("")


def test_plan_marker_wins():
    from slcloud.adapters import is_question
    assert not is_question("Plan ready. Reply approve to start\nReply to answer")
