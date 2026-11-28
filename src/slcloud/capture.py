"""Turn agent output into short, masked, throttled run events."""

from __future__ import annotations

import re
from typing import Callable

from slcloud.adapters import _SECRET, redact
from slcloud.vault import mask

MAX_TEXT = 4000
SANDBOX_ROOT = "/work/"


def rel(path):
    """Show a sandbox path relative to the checkout: /work/api.py -> api.py."""
    if isinstance(path, str) and path.startswith(SANDBOX_ROOT) and len(path) > len(SANDBOX_ROOT):
        return path[len(SANDBOX_ROOT):]
    return path
_KINDS = {"text", "thinking", "tool", "file", "status", "agent"}
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
        self._pending: list[tuple[str, str]] = []

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

    def _spans(self, joined: str) -> list[tuple[int, int]]:
        spans = [m.span() for m in _SECRET.finditer(joined)]
        for sec in self.secrets:
            if not sec:
                continue
            i = joined.find(sec)
            while i != -1:
                spans.append((i, i + len(sec)))
                i = joined.find(sec, i + 1)

        # Merge overlapping matches so masking a crossing match cannot expose another match.
        merged = []
        for start, end in sorted(spans):
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return merged

    def _flush_pending(self, count: int | None = None) -> None:
        if not self._pending:
            return
        pending = self._pending
        joined = "".join(raw for _, raw in pending)
        merged = self._spans(joined)
        boundaries, offset = [], 0
        for _, raw in pending[:-1]:
            offset += len(raw)
            boundaries.append(offset)
        crossing = [(start, end) for start, end in merged
                    if any(start < boundary < end for boundary in boundaries)]

        offset = 0
        count = len(pending) if count is None else count
        self._pending = pending[count:]
        for kind, raw in pending[:count]:
            end = offset + len(raw)
            pieces, cursor = [], offset
            for start, stop in crossing:
                if raw and start < end and stop > offset:
                    pieces.extend((joined[cursor:max(start, offset)], "***"))
                    cursor = min(stop, end)
            pieces.append(joined[cursor:end])
            self._emit_text(kind, self._clean("".join(pieces)))
            offset = end

    def feed(self, event: dict) -> None:
        kind = event.get("kind")
        if kind not in _KINDS:
            return
        if kind in _TEXTY:
            self._pending.append((kind, str(event.get("text", ""))))
            joined = "".join(raw for _, raw in self._pending)
            held = self._held(joined)
            if not held:
                self._flush_pending()
            else:
                limit = len(joined) - held
                spans = self._spans(joined)
                offset, count = 0, 0
                for i, (_, raw) in enumerate(self._pending):
                    offset += len(raw)
                    if 0 < offset <= limit and not any(
                        start < offset < end for start, end in spans
                    ):
                        count = i + 1
                if count:
                    self._flush_pending(count)
            return
        self._flush_pending()
        data = {k: self._clean(rel(v) if k in ("target", "path") else v, 500)
                for k, v in event.items() if k != "kind"}
        self._emit(kind, data)

    def flush(self) -> None:
        self._flush_pending()
