"""Turn agent output into short, masked, throttled run events."""

from __future__ import annotations

import re
from typing import Callable

from slcloud.adapters import redact
from slcloud.vault import mask

MAX_TEXT = 4000
SANDBOX_ROOT = "/work/"


def rel(path):
    """Show a sandbox path relative to the checkout: /work/api.py -> api.py."""
    if isinstance(path, str) and path.startswith(SANDBOX_ROOT) and len(path) > len(SANDBOX_ROOT):
        return path[len(SANDBOX_ROOT):]
    return path
_KINDS = {"text", "thinking", "tool", "file", "status"}
_TEXTY = ("text", "thinking")
# A trailing key that may still grow, or a trailing start of a key marker.
_OPEN_KEY = re.compile(r"\b(?:sk-|key-|AIza|ghp_)[A-Za-z0-9_\-]*\Z")
_OPEN_MARK = re.compile(r"\b(?:s|sk|k|ke|key|A|AI|AIz|g|gh|ghp)\Z")


class Capture:
    def __init__(self, task_id: str, add_event: Callable[[str, str, dict], None],
                 secrets: list[str], clock: Callable[[], float], min_gap: float = 0.25):
        self.task_id, self.add_event = task_id, add_event
        self.secrets, self.clock, self.min_gap = list(secrets), clock, min_gap
        self.step = 0
        self._carry = ""  # raw block whose tail may be the start of a secret split across chunks
        self._carry_kind = "text"

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

    def _emit_text(self, kind: str, text: str) -> None:
        if text:
            self._emit(kind, {"text": text[:MAX_TEXT]})

    def _flush_carry(self) -> None:
        """Emit the held tail (masked) as its own event of the kind it came from."""
        if self._carry:
            carry, kind = self._carry, self._carry_kind
            self._carry = ""
            self._emit_text(kind, self._clean(carry))

    def feed(self, event: dict) -> None:
        kind = event.get("kind")
        if kind not in _KINDS:
            return
        if kind in _TEXTY:
            # Each event is one complete message block. A block whose tail may start a split secret is
            # held whole; the next event of the same kind either stands apart or joins it if a secret straddles.
            if self._carry_kind != kind:
                self._flush_carry()
            self._carry_kind = kind
            data = str(event.get("text", ""))
            if self._carry:
                prev, self._carry = self._carry, ""
                joined = prev + data
                if (self._held(joined) <= len(data)
                        and self._clean(joined) == self._clean(prev) + self._clean(data)):
                    self._emit_text(kind, self._clean(prev))
                else:
                    data = joined
            if self._held(data):
                self._carry = data
                return
            self._emit_text(kind, self._clean(data))
            return
        self._flush_carry()
        data = {k: self._clean(rel(v) if k in ("target", "path") else v, 500)
                for k, v in event.items() if k != "kind"}
        self._emit(kind, data)

    def flush(self) -> None:
        self._flush_carry()
