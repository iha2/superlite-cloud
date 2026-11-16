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
    """`base` if free, else a numbered variant that check_name still accepts (at most 3 words)."""
    if base not in taken:
        return base
    words = base.split("_")
    if len(words) >= 2 and words[-1].isdigit():  # already suffixed: bump the number
        stem, n = "_".join(words[:-1]), int(words[-1]) + 1
        join = "_"
    elif len(words) < 3:
        stem, n, join = base, 2, "_"
    else:  # three words: no room for a fourth, so number the last word
        stem, n, join = base, 2, ""
    while f"{stem}{join}{n}" in taken:
        n += 1
    return f"{stem}{join}{n}"
