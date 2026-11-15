"""Run names: 1-3 words of [a-z0-9] joined by underscores."""
from __future__ import annotations

import re

STOP = {"the", "a", "an", "for", "of", "to", "in", "on", "and", "with", "my", "our"}
VERBS = {"fix", "add", "remove", "update", "make", "build", "change", "refactor", "improve",
         "rename", "delete", "support"}
_NAME = re.compile(r"[a-z0-9]+(_[a-z0-9]+){0,2}")


def make_name(task: str) -> str:
    words = re.findall(r"[a-z0-9]+", task.lower())
    if not words:
        return "run"
    first = words[0]
    content = [w for w in words if w not in STOP]
    if first in VERBS:
        rest = content[1:]
        if rest:
            return f"{rest[0]}_{first}"
        return first
    return "_".join(content[:2]) or "run"


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("name must be 1-3 words of a-z0-9 joined by _")
    return name


def slug(name: str) -> str:
    return name.replace("_", "-")


def free_name(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"
