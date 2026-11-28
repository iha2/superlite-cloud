"""AgentTracker: Claude Code stream-json lines to `agent` events."""

import time
from typing import Callable

_TIERS = ("haiku", "sonnet", "opus")


def _tier(*names) -> str:
    for n in names:
        if isinstance(n, str):
            for t in _TIERS:
                if t in n:
                    return t
    return "?"


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


class AgentTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic, gap: float = 5.0) -> None:
        self._clock = clock
        self._gap = gap
        self._blocks: dict[str, dict] = {}
        self._running: dict[str, dict] = {}
        self._ended: set[str] = set()
        self._seen_msgs: set[str] = set()
        self._last: dict[str, float] = {}

    def feed(self, line: dict) -> list[dict]:
        try:
            return self._feed(line)
        except Exception:
            return []

    def _feed(self, line) -> list[dict]:
        if not isinstance(line, dict):
            return []
        kind = line.get("type")
        if kind == "assistant":
            return self._assistant(line)
        if kind == "system":
            sub = line.get("subtype")
            if sub == "task_started":
                return self._started(line)
            if sub == "task_notification":
                return self._notified(line)
        elif kind == "user":
            return self._user_result(line)
        return []

    def _assistant(self, line: dict) -> list[dict]:
        msg = line.get("message")
        if not isinstance(msg, dict):
            return []
        parent = line.get("parent_tool_use_id")
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent" \
                        and isinstance(b.get("id"), str):
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    self._blocks[b["id"]] = {
                        "parent": parent if isinstance(parent, str) else None,
                        "task": inp.get("description"),
                        "type": inp.get("subagent_type"),
                        "model": inp.get("model"),
                    }
        if not isinstance(parent, str) or parent not in self._running:
            return []
        info = self._running[parent]
        mid = msg.get("id")
        usage = msg.get("usage")
        if info["tier"] == "?":
            info["tier"] = _tier(msg.get("model"))
        if not isinstance(usage, dict) or not isinstance(mid, str) or mid in self._seen_msgs:
            return []
        self._seen_msgs.add(mid)
        info["tokens"] += _int(usage.get("input_tokens")) + _int(usage.get("output_tokens"))
        now = self._clock()
        last = self._last.get(parent)
        if info["tokens"] != info["sent"] and (last is None or now - last >= self._gap):
            self._last[parent] = now
            info["sent"] = info["tokens"]
            return [{"kind": "agent", "id": parent, "state": "update", "tokens": info["tokens"]}]
        return []

    def _started(self, line: dict) -> list[dict]:
        i = line.get("tool_use_id")
        if not isinstance(i, str) or i in self._running or i in self._ended:
            return []
        blk = self._blocks.get(i, {})
        task = blk.get("task") or line.get("description") or ""
        typ = blk.get("type") or line.get("subagent_type") or ""
        typ = typ.rsplit(":", 1)[-1] if isinstance(typ, str) else ""
        tier = _tier(blk.get("model"))
        self._running[i] = {"tier": tier, "tokens": 0, "sent": 0}
        return [{"kind": "agent", "id": i, "parent": blk.get("parent"), "type": typ,
                 "tier": tier, "task": task if isinstance(task, str) else "",
                 "state": "start", "tokens": 0}]

    def _end(self, i, state: str, tokens: int) -> list[dict]:
        if i not in self._running or i in self._ended:
            return []
        del self._running[i]
        self._ended.add(i)
        return [{"kind": "agent", "id": i, "state": state, "tokens": tokens}]

    def _notified(self, line: dict) -> list[dict]:
        status = line.get("status")
        state = {"completed": "done", "failed": "failed", "stopped": "failed"}.get(status)
        if state is None:
            return []
        usage = line.get("usage")
        tokens = _int(usage.get("total_tokens")) if isinstance(usage, dict) else 0
        return self._end(line.get("tool_use_id"), state, tokens)

    def _user_result(self, line: dict) -> list[dict]:
        res = line.get("tool_use_result")
        if not isinstance(res, dict) or res.get("status") != "completed":
            return []
        msg = line.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        out: list[dict] = []
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out += self._end(b.get("tool_use_id"), "done", _int(res.get("totalTokens")))
        return out
