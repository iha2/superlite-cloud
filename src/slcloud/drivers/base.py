"""Driver base class: one long-lived agent process per run."""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from slcloud.adapters import AgentResult, _result

if TYPE_CHECKING:
    from slcloud.sandbox import Proc


def _json(line: str) -> dict | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class Driver:
    name = ""
    supports_nudge = True

    def __init__(self, start: Callable[[list[str]], Proc], session: str | None = None,
                 slug: str | None = None, workdir: str = "/work") -> None:
        self._start = start
        self.workdir = workdir  # where the agent runs: /work in a container, the run dir under `process`
        self.session = session
        self.slug = slug  # the superlite session the worker opened in /work for this run
        self.proc: Proc | None = None
        self.stopped = False
        self._text: str | None = None
        self._ok = True

    def argv(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def user_line(self, text: str) -> str:
        raise NotImplementedError

    def start(self, prompt: str) -> None:
        self.proc = self._start(self.argv(prompt))
        self.proc.write(self.user_line(prompt))

    def lines(self) -> Iterator[str]:
        if self.proc is None:
            return
        for line in self.proc.lines():
            self.observe(line)
            yield line

    def nudge(self, text: str) -> str:
        if self.proc is None or self.stopped or not self.supports_nudge:
            return "unsupported"
        self.proc.write(self.user_line(text))
        return "delivered"

    def stop(self) -> None:
        self.stopped = True
        if self.proc is not None:
            self.proc.close_stdin()

    def observe(self, line: str) -> None:
        """Remember the final result and session from one output line."""

    def events(self, line: str) -> list[dict]:
        return []

    def result(self) -> AgentResult:
        if self._text is None:
            return AgentResult("", self.session, None, False)
        return _result(self._text, self.session, self._ok)
