"""Helpers for live tests that drive the real claude CLI. Importing this module does no work."""

from __future__ import annotations

import os
import shutil
import subprocess
import time

_HUMAN_PROMPT = (
    "You are a human product owner answering a developer's question about a small task. "
    "Reply with one short line that answers the question and nothing else.\n\nQuestion:\n"
)


def live_ready() -> tuple[bool, str]:
    """True only when SLCLOUD_LIVE=1 and the claude CLI is on PATH."""
    if os.environ.get("SLCLOUD_LIVE") != "1":
        return False, "set SLCLOUD_LIVE=1 to run live tests"
    if shutil.which("claude") is None:
        return False, "claude CLI not found on PATH"
    return True, ""


def ask_ai_human(question: str, timeout: float = 120) -> str:
    """Run a second `claude -p` as the human; return its answer as one line. Never logs the environment."""
    try:
        r = subprocess.run(["claude", "-p", _HUMAN_PROMPT + question], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"claude -p gave no answer within {timeout}s") from None
    if r.returncode != 0:
        # report the exit code only: stderr may echo credentials
        raise RuntimeError(f"claude -p exited {r.returncode}")
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("claude -p returned no answer")
    return lines[0]


def wait_for(client, run: str, pred, timeout: float, headers: dict | None = None, poll: float = 0.5) -> dict:
    """Poll GET /api/tasks/<run> until pred(task_json) holds; TimeoutError after `timeout` seconds."""
    end = time.monotonic() + timeout
    last = None
    while True:
        resp = client.get(f"/api/tasks/{run}", headers=headers or {})
        if resp.status_code == 200:
            last = resp.json()
            if pred(last):
                return last
        if time.monotonic() >= end:
            status = last.get("status") if last else None
            raise TimeoutError(f"run {run} did not reach the condition in {timeout}s (status {status})")
        time.sleep(poll)
