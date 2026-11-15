"""Gemini driver: `gemini --experimental-acp` (Agent Client Protocol) over stdio."""
from __future__ import annotations

from slcloud.drivers.codex import RpcDriver, RpcError, _parse


class GeminiDriver(RpcDriver):
    cmd = ["gemini", "--experimental-acp"]
    cwd = "/work"

    def _prompt(self, text: str) -> int:
        rid = self._send("session/prompt", {"sessionId": self.session,
                                            "prompt": [{"type": "text", "text": text}]},
                         track=False)
        self._last_prompt = rid
        return rid

    def start(self, prompt: str) -> None:
        self._last_prompt = 0
        self._launch()
        self._call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        params = {"cwd": self.cwd, "mcpServers": []}
        if self.session:
            self._call("session/load", {"sessionId": self.session, **params})
        else:
            self.session = self._call("session/new", params).get("sessionId")
        self._prompt(prompt)

    def nudge(self, text: str) -> str:
        if self.proc is None or self.stopped:
            return "unsupported"
        try:
            self._send("session/cancel", {"sessionId": self.session}, notify=True)
            self._prompt(text)
        except Exception:
            return "unsupported"
        return "delivered"

    def _is_end(self, msg) -> bool:
        return (bool(msg) and "method" not in msg and msg.get("id") == self._last_prompt
                and msg.get("id") is not None)

    def _finished(self, msg) -> bool:
        return self._is_end(msg)

    def events(self, line: str) -> list[dict]:
        msg = _parse(line)
        if not msg:
            return []
        if "method" not in msg:
            if msg.get("id") == self._last_prompt:
                if "error" in msg:
                    self._ok = False
                    return [{"kind": "error", "text": str(msg["error"].get("message", ""))}]
                return [{"kind": "done"}]
            return []  # a cancelled earlier prompt
        if msg["method"] != "session/update":
            return []
        u = (msg.get("params") or {}).get("update") or {}
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            t = (u.get("content") or {}).get("text", "")
            self._add_text(t)
            return [{"kind": "text", "text": t}]
        if kind == "tool_call":
            loc = (u.get("locations") or [{}])[0].get("path") or u.get("title", "")
            return [{"kind": "tool", "tool": u.get("kind") or "tool", "target": loc}]
        return []
