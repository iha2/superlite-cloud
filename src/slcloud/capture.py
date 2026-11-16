"""Turn agent output into short, masked, throttled run events."""

from __future__ import annotations

import re
from typing import Callable

from slcloud.adapters import redact
from slcloud.vault import mask

MAX_TEXT = 200
SANDBOX_ROOT = "/work/"


def rel(path):
    """Show a sandbox path relative to the checkout: /work/api.py -> api.py."""
    if isinstance(path, str) and path.startswith(SANDBOX_ROOT) and len(path) > len(SANDBOX_ROOT):
        return path[len(SANDBOX_ROOT):]
    return path
_KINDS = {"text", "tool", "file", "status"}
# A trailing key that may still grow, or a trailing start of a key marker.
_OPEN_KEY = re.compile(r"\b(?:sk-|key-|AIza|ghp_)[A-Za-z0-9_\-]*\Z")
_OPEN_MARK = re.compile(r"\b(?:s|sk|k|ke|key|A|AI|AIz|g|gh|ghp)\Z")


class Capture:
    def __init__(self, task_id: str, add_event: Callable[[str, str, dict], None],
                 secrets: list[str], clock: Callable[[], float], min_gap: float = 0.25):
        self.task_id, self.add_event = task_id, add_event
        self.secrets, self.clock, self.min_gap = list(secrets), clock, min_gap
        self.step = 0
        self._pending: str | None = None
        self._carry = ""  # raw tail that may be the start of a secret split across chunks
        self._since = 0.0

    def _held(self, data: str) -> int:
        """Length of the tail of data that could still grow into a secret."""
        n = 0
        for rx in (_OPEN_KEY, _OPEN_MARK):
            m = rx.search(data)
            if m:
                n = max(n, len(m.group(0)))
        for sec in self.secrets:
            for k in range(min(len(sec) - 1, len(data)), n, -1):
                if data.endswith(sec[:k]):
                    n = k
                    break
        return n

    def _clean(self, value, limit: int | None = None):
        if not isinstance(value, str):
            return value
        out = redact(mask(value, self.secrets))
        return out[:limit] if limit else out

    def _emit(self, kind: str, data: dict) -> None:
        self.step += 1
        self.add_event(self.task_id, kind, {"step": self.step, **data})

    def _emit_text(self) -> None:
        if self._pending is not None or self._carry:
            text = (self._pending or "") + self._clean(self._carry)
            self._pending, self._carry = None, ""
            self._emit("text", {"text": text[:MAX_TEXT]})

    def feed(self, event: dict) -> None:
        kind = event.get("kind")
        if kind not in _KINDS:
            return
        if kind == "text":
            data = self._carry + str(event.get("text", ""))
            cut = len(data) - self._held(data)
            self._carry = data[cut:]
            if cut == 0 and self._carry:
                return  # the whole chunk may be part of a secret; wait for the next one
            text = self._clean(data[:cut])
            if self._pending is None:
                self._since = self.clock()
            self._pending = text
            if self.clock() - self._since >= self.min_gap:
                self._emit_text()
            return
        self._emit_text()
        data = {k: self._clean(rel(v) if k in ("target", "path") else v, 500)
                for k, v in event.items() if k != "kind"}
        self._emit(kind, data)

    def flush(self) -> None:
        self._emit_text()
