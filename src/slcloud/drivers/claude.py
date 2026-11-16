"""Claude driver: stream-json in and out over one long-lived process."""
from __future__ import annotations

import json

from slcloud.drivers.base import Driver, _json

_TARGETS = ("file_path", "path", "command", "pattern", "url", "query")


class ClaudeDriver(Driver):
    name = "claude"

    def argv(self, prompt: str) -> list[str]:
        argv = ["claude", "-p", "--bare", "--plugin-dir", "/opt/superlite",
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--permission-mode", "acceptEdits"]
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
        if not e:
            return []
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
            elif block.get("type") == "tool_use":
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                target = next((str(inp[k]) for k in _TARGETS if k in inp), "")
                out.append({"kind": "tool", "tool": str(block.get("name", "")), "target": target})
        return out
