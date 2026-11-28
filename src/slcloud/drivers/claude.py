"""Claude driver: stream-json in and out over one long-lived process."""
from __future__ import annotations

import json

from slcloud.adapters import CLAUDE_ISOLATION
from slcloud.drivers.agents import AgentTracker
from slcloud.drivers.base import Driver, _json

_TARGETS = ("file_path", "path", "command", "pattern", "url", "query")
NO_WORKTREE_TOOLS = ("EnterWorktree", "ExitWorktree")


def headless_note(slug: str | None, workdir: str = "/work") -> str:
    """System-prompt note: keep superlite in the run's working directory, where the worker opened its
    session and hands back from (/work in a container, the run directory under the process runner)."""
    session = f"`{slug}`" if slug else f"the one already open in {workdir}"
    return ("This is a headless superlite-cloud run inside a sandbox; nobody can answer questions mid-run. "
            f"The superlite session for this run is already open in {workdir} on its branch: its slug is {session}. "
            f"Build everything in {workdir} on that branch. Never create, enter or switch to a git worktree or "
            f"another session (no `session create`, no `--worktree`), because only {workdir} is handed back. "
            f"Whenever superlite asks you to ensure or create a session, run `session ensure --slug {slug or '<that slug>'}`"
            f" in {workdir} instead. Leave your changes in {workdir}; the run commits and hands them back.")


class ClaudeDriver(Driver):
    name = "claude"

    def argv(self, prompt: str) -> list[str]:
        # Same isolation as ClaudeAdapter (see adapters.CLAUDE_ISOLATION and SECURITY.md).
        argv = ["claude", "-p", *CLAUDE_ISOLATION,
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--permission-mode", "bypassPermissions",
                # Hand-back commits the run's working directory only: work done in a worktree would be lost.
                "--disallowedTools", *NO_WORKTREE_TOOLS,
                "--append-system-prompt", headless_note(self.slug, self.workdir)]
        if self.session:
            argv += ["--resume", self.session]
        return argv

    def user_line(self, text: str) -> str:
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        return json.dumps(msg, separators=(",", ":")) + "\n"

    def observe(self, line: str) -> None:
        e = _json(line)
        if not e:
            return
        if isinstance(e.get("session_id"), str):
            self.session = e["session_id"]
        if e.get("type") == "result" and isinstance(e.get("result"), str):
            self._text, self._ok = e["result"], not e.get("is_error", False)

    def events(self, line: str) -> list[dict]:
        e = _json(line)
        if not isinstance(e, dict) or not e:
            return []
        if getattr(self, "_agents", None) is None:
            self._agents = AgentTracker()
        return self._line_events(e) + self._agents.feed(e)

    def _line_events(self, e: dict) -> list[dict]:
        if e.get("type") == "result":
            return [{"kind": "status", "status": "error" if e.get("is_error") else "done"}]
        msg = e.get("message")
        if e.get("type") != "assistant" or not isinstance(msg, dict):
            return []
        out: list[dict] = []
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                out.append({"kind": "text", "text": block["text"]})
            elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str) and block["thinking"]:
                out.append({"kind": "thinking", "text": block["thinking"]})
            elif block.get("type") == "tool_use":
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                target = next((str(inp[k]) for k in _TARGETS if k in inp), "")
                out.append({"kind": "tool", "tool": str(block.get("name", "")), "target": target})
        return out
