"""Codex driver: `codex app-server` JSON-RPC over stdio, plus the shared RPC plumbing."""
from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from slcloud.drivers.base import Driver

if TYPE_CHECKING:
    from slcloud.sandbox import Proc

TIMEOUT_S = 30.0


class RpcError(Exception):
    pass


class RpcDriver(Driver):
    """Line-delimited JSON-RPC client. A reader thread resolves responses to our
    requests and queues everything else for lines()."""

    cmd: list[str] = []
    timeout_s = TIMEOUT_S

    def __init__(self, start: Callable[[list[str]], Proc], session: str | None = None) -> None:
        super().__init__(start, session)
        self._q: queue.Queue[str | None] = queue.Queue()
        self._waiters: dict[int, tuple[threading.Event, list]] = {}
        self._lock = threading.Lock()
        self._id = 0

    # -- plumbing
    def _send(self, method: str, params: dict | None = None, *, notify: bool = False,
              track: bool = True, on_id: Callable[[int], None] | None = None) -> int:
        with self._lock:
            msg: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            rid = 0
            if not notify:
                self._id += 1
                rid = msg["id"] = self._id
                if track:
                    self._waiters[rid] = (threading.Event(), [])
                if on_id is not None:
                    on_id(rid)  # registered before the request can be answered
            self.proc.write(json.dumps(msg) + "\n")
        return rid

    def _call(self, method: str, params: dict | None = None) -> dict:
        rid = self._send(method, params)
        ev, box = self._waiters[rid]
        if not ev.wait(self.timeout_s) or not box:
            self._waiters.pop(rid, None)
            raise RpcError(f"{method}: no response")
        self._waiters.pop(rid, None)
        msg = box[0]
        if "error" in msg:
            raise RpcError(f"{method}: {msg['error']}")
        return msg.get("result") or {}

    def _read(self) -> None:
        try:
            for line in self.proc.lines():
                try:
                    msg = json.loads(line)
                except ValueError:
                    msg = None
                if isinstance(msg, dict) and msg.get("id") in self._waiters and "method" not in msg:
                    ev, box = self._waiters[msg["id"]]
                    box.append(msg)
                    ev.set()
                    continue
                self._q.put(line)
                if self._finished(msg):
                    break
        except Exception:
            self._ok = False
        finally:
            for ev, _ in list(self._waiters.values()):
                ev.set()
            self._q.put(None)

    def _finished(self, msg) -> bool:
        return False

    def _launch(self) -> None:
        self.proc = self._start(list(self.cmd))
        threading.Thread(target=self._read, daemon=True).start()

    # -- Driver API
    def lines(self) -> Iterator[str]:
        while True:
            line = self._q.get()
            if line is None:
                return
            yield line

    def stop(self) -> None:
        super().stop()
        if self.proc is not None:
            self.proc.kill()

    def _add_text(self, t: str) -> None:
        self._text = (self._text or "") + t


def _parse(line: str):
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


class CodexDriver(RpcDriver):
    cmd = ["codex", "app-server"]

    def start(self, prompt: str) -> None:
        self._launch()
        try:
            self._call("initialize", {"clientInfo": {"name": "slcloud", "version": "1"}})
            self._send("initialized", notify=True)
            if self.session:
                res = self._call("thread/resume", {"threadId": self.session})
            else:
                res = self._call("thread/start", {})
            self.session = (res.get("thread") or {}).get("id") or self.session
            self._send("turn/start", {"threadId": self.session,
                                      "input": [{"type": "text", "text": prompt}]}, track=False)
        except BaseException:
            self.stop()  # the sandbox is already running; never leave it behind
            raise

    def nudge(self, text: str) -> str:
        if self.proc is None or self.stopped:
            return "unsupported"
        try:
            self._call("turn/steer", {"threadId": self.session,
                                      "input": [{"type": "text", "text": text}]})
        except RpcError as e:
            return "unsupported" if "no response" not in str(e) else "failed"
        except Exception:
            return "failed"
        return "delivered"

    def _finished(self, msg) -> bool:
        return bool(msg) and msg.get("method") == "turn/completed"

    def events(self, line: str) -> list[dict]:
        msg = _parse(line)
        if not msg:
            return []
        method, p = msg.get("method"), msg.get("params") or {}
        item = p.get("item") or {}
        kind = item.get("type")
        if method == "item/started":
            if kind == "commandExecution":
                return [{"kind": "tool", "tool": "Bash", "target": item.get("command", "")}]
            if kind == "fileChange":
                ch = item.get("changes") or [{}]
                return [{"kind": "tool", "tool": "Edit", "target": ch[0].get("path", "")}]
        elif method == "item/completed" and kind == "agentMessage":
            self._add_text(item.get("text", ""))
            return [{"kind": "text", "text": item.get("text", "")}]
        elif method == "turn/completed":
            return [{"kind": "done"}]
        return []
