"""Agent CLI adapters: build commands and parse their output."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

PLAN_MARKER = "Reply approve to start"


@dataclass
class AgentResult:
    text: str
    session: str | None
    plan: str | None
    ok: bool


def _result(text: str, session: str | None, ok: bool = True) -> AgentResult:
    plan = text if ok and PLAN_MARKER in text else None
    return AgentResult(text, session, plan, ok)


_SECRET = re.compile(r"\b(?:sk-|key-|AIza|ghp_)[A-Za-z0-9_\-]{16,}")


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


def _bad(stdout: str) -> AgentResult:
    return AgentResult(redact(stdout.strip()[:2000]), None, None, False)


def _pos(prompt: str) -> list[str]:
    """Positional prompt; '--' stops option parsing when it starts with '-'."""
    return ["--", prompt] if prompt.startswith("-") else [prompt]


def _val(prompt: str) -> str:
    """Option value; leading space keeps it from being read as a flag."""
    return " " + prompt if prompt.startswith("-") else prompt


def _events(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _whole(stdout: str) -> dict | None:
    try:
        obj = json.loads(stdout)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class AgentAdapter:
    name = ""
    provider = ""
    key_env = ""
    supports_resume = False

    def command(self, prompt: str, resume: str | None) -> list[str]:
        if resume and not self.supports_resume:
            raise ValueError(f"{self.name} does not support resume")
        return self._command(prompt, resume)

    def _command(self, prompt: str, resume: str | None) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str) -> AgentResult:
        raise NotImplementedError


class ClaudeAdapter(AgentAdapter):
    name, provider, key_env, supports_resume = "claude", "anthropic", "ANTHROPIC_API_KEY", True

    def _command(self, prompt, resume):
        argv = ["claude", "-p", "--bare", "--plugin-dir", "/opt/superlite",
                "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits"]
        if resume:
            argv += ["--resume", resume]
        return argv + _pos(prompt)

    def parse(self, stdout):
        final = [e for e in _events(stdout) if e.get("type") == "result"]
        if not final:
            return _bad(stdout)
        e = final[-1]
        text = e.get("result")
        if not isinstance(text, str):
            return _bad(stdout)
        session = e.get("session_id")
        return _result(text, session if isinstance(session, str) else None,
                       not e.get("is_error", False))


class CodexAdapter(AgentAdapter):
    name, provider, key_env, supports_resume = "codex", "openai", "OPENAI_API_KEY", True

    def _command(self, prompt, resume):
        if resume:
            return ["codex", "exec", "resume", resume, "--json", "--full-auto", *_pos(prompt)]
        return ["codex", "exec", "--json", "--full-auto", "--skip-git-repo-check", *_pos(prompt)]

    def parse(self, stdout):
        session = text = None
        for e in _events(stdout):
            if e.get("type") == "thread.started" and isinstance(e.get("thread_id"), str):
                session = e["thread_id"]
            elif e.get("type") == "item.completed":
                item = e.get("item")
                if (isinstance(item, dict) and item.get("type") == "agent_message"
                        and isinstance(item.get("text"), str)):
                    text = item["text"]
        if text is None:
            return _bad(stdout)
        return _result(text, session)


class GeminiAdapter(AgentAdapter):
    name, provider, key_env = "gemini", "gemini", "GEMINI_API_KEY"

    def _command(self, prompt, resume):
        return ["gemini", "-p", _val(prompt), "--output-format", "json", "--yolo"]

    def parse(self, stdout):
        obj = _whole(stdout)
        if not obj or not isinstance(obj.get("response"), str):
            return _bad(stdout)
        return _result(obj["response"], None)


class CursorAdapter(AgentAdapter):
    name, provider, key_env = "cursor", "cursor", "CURSOR_API_KEY"

    def _command(self, prompt, resume):
        return ["cursor-agent", "-p", _val(prompt), "--output-format", "json", "--force"]

    def parse(self, stdout):
        obj = _whole(stdout)
        if not obj or not isinstance(obj.get("result"), str):
            return _bad(stdout)
        session = obj.get("session_id")
        return _result(obj["result"], session if isinstance(session, str) else None,
                       not obj.get("is_error", False))


_ADAPTERS = {a.name: a for a in (ClaudeAdapter(), CodexAdapter(), GeminiAdapter(), CursorAdapter())}


def get_adapter(name: str) -> AgentAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise ValueError(f"unknown agent: {name}") from None
