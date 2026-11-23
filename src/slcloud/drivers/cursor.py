"""Cursor driver: one process per turn, nudges queue and resume the same chat."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

from slcloud.drivers.base import Driver, _json

_TARGETS = ("path", "file_path", "command", "pattern")


class CursorDriver(Driver):
    name = "cursor"

    def __init__(self, start, session: str | None = None, slug: str | None = None) -> None:
        super().__init__(start, session, slug)
        self.queue: list[str] = []
        self._qlock = threading.Lock()
        # Called with the number of queued nudges once the resumed turn has started.
        self.on_resume: Callable[[int], None] | None = None

    def argv(self, prompt: str) -> list[str]:
        argv = ["cursor-agent", "-p", "--force", "--output-format", "stream-json"]
        if self.session:
            argv += ["--resume", self.session]
        return argv + ["--", prompt]

    def user_line(self, text: str) -> str:
        return text

    def start(self, prompt: str) -> None:
        self.proc = self._start(self.argv(prompt))

    def nudge(self, text: str) -> str:
        if self.stopped or self.proc is None:
            return "unsupported"
        with self._qlock:
            if self.stopped:
                return "unsupported"
            self.queue.append(text)
        return "queued"

    def lines(self) -> Iterator[str]:
        while self.proc is not None:
            for line in self.proc.lines():
                self.observe(line)
                yield line
            with self._qlock:
                if self.stopped or not self.queue:
                    return
                batch, self.queue = self.queue, []
            self.proc = self._start(self.argv("\n\n".join(batch)))
            if self.on_resume is not None:
                self.on_resume(len(batch))

    def stop(self) -> None:
        with self._qlock:
            self.stopped = True
            self.queue = []

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
            with self._qlock:
                resuming = bool(self.queue) and not self.stopped
            if resuming and not e.get("is_error"):
                return []  # a queued nudge resumes the chat; this turn is not the end
            return [{"kind": "status", "status": "error" if e.get("is_error") else "done"}]
        if e.get("type") == "assistant":
            content = (e.get("message") or {}).get("content") or []
            return [{"kind": "text", "text": b["text"]} for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        if e.get("type") == "tool_call" and e.get("subtype") == "started":
            call = e.get("tool_call") or {}
            name = next(iter(call), "tool") if isinstance(call, dict) else "tool"
            args = (call.get(name) or {}).get("args") if isinstance(call.get(name), dict) else {}
            args = args if isinstance(args, dict) else {}
            target = next((str(args[k]) for k in _TARGETS if k in args), "")
            return [{"kind": "tool", "tool": name, "target": target}]
        return []
