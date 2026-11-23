"""Agent drivers."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

# Submodules import `slcloud.drivers.base` themselves and nothing here, so loading them eagerly is cycle-free.
from slcloud.drivers import base, claude, codex, cursor, gemini
from slcloud.drivers.claude import ClaudeDriver
from slcloud.drivers.codex import CodexDriver
from slcloud.drivers.cursor import CursorDriver
from slcloud.drivers.gemini import GeminiDriver

if TYPE_CHECKING:
    from slcloud.drivers.base import Driver
    from slcloud.sandbox import Proc

__all__ = ["base", "claude", "codex", "cursor", "gemini", "get_driver",
           "ClaudeDriver", "CodexDriver", "CursorDriver", "GeminiDriver"]

_DRIVERS = {"claude": ClaudeDriver, "cursor": CursorDriver, "codex": CodexDriver, "gemini": GeminiDriver}


def get_driver(agent: str, start: Callable[[list[str]], Proc], session: str | None = None,
               slug: str | None = None) -> Driver:
    cls = _DRIVERS.get(agent)
    if cls is None:
        raise ValueError(f"unknown agent: {agent}")
    return cls(start, session, slug=slug)
