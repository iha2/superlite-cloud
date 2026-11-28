"""Live: a Human-orchestrated run on the real claude driver, with a second claude playing the human.

Slow and opt-in: SLCLOUD_LIVE=1, `claude` on PATH and ANTHROPIC_API_KEY set (the process sandbox has no
claude login). Excluded by `-m "not slow"`.
"""

import os
import stat
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from slcloud import app as slapp
from slcloud import members
from slcloud.config import load_settings
from slcloud.sandbox import ProcessRunner
from slcloud.vault import Vault
from slcloud.worker import loop_once

from live_helpers import ask_ai_human, live_ready, wait_for

_ok, _why = live_ready()
if _ok and not os.environ.get("ANTHROPIC_API_KEY"):
    _ok, _why = False, "set ANTHROPIC_API_KEY for the live run"

pytestmark = [pytest.mark.slow, pytest.mark.skipif(not _ok, reason=_why)]

GIT = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}
# The superlite CLI is not the thing under test; this stand-in only creates the session branch.
FAKE_CLI = """\
import subprocess, sys
a = sys.argv[1:]
def g(*x): subprocess.run(["git", *x], check=True)
if a[1] == "create":
    g("checkout", "-q", "-b", "superlite/sessions/" + a[a.index("--slug") + 1])
elif a[1] == "rename":
    g("branch", "-m", "superlite/sessions/" + a[a.index("--slug") + 1], "superlite/sessions/" + a[a.index("--to") + 1])
"""
PROMPT = ("Create greeting.txt in the repo. Before you write anything, ask me exactly one question: "
          "should it say hello or goodbye? Then follow my answer.")


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, env=GIT, check=True, capture_output=True, text=True).stdout


def test_live_human_orchestrated_run(tmp_path):
    seed = tmp_path / "seed"
    git("init", "-q", "-b", "main", str(seed))
    (seed / "a.txt").write_text("a")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "init", cwd=seed)
    remote = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(seed), str(remote))
    cli = tmp_path / "fakecli.py"
    cli.write_text(FAKE_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)

    st = load_settings({"SLCLOUD_DATA": str(tmp_path / "d"), "SLCLOUD_MASTER_KEY": "k",
                        "SLCLOUD_SANDBOX": "process"})
    app = slapp.build(st, runner=ProcessRunner())
    app.state.worker_deps.cli = [sys.executable, str(cli)]
    app.state.worker_deps.poll = 0.1
    tok, _ = slapp.init_workspace(st, "team", member="chris")
    store = app.state.store
    ws = store.query("SELECT workspace FROM members WHERE handle='chris'")[0]["workspace"]
    Vault(store, "k").set(ws, "anthropic", os.environ["ANTHROPIC_API_KEY"])
    h = {"Authorization": f"Bearer {tok}"}

    with TestClient(app) as client:
        r = client.post("/api/tasks", headers=h, json={"repo": str(remote), "prompt": PROMPT, "base": "main",
                                                       "name": "login_fix", "orchestrator": "human"})
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        questions = 0
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline:
            if loop_once(app.state.worker_deps):
                pass
            t = client.get(f"/api/tasks/{tid}", headers=h).json()
            if t["status"] in ("done", "failed", "cancelled"):
                break
            if t["status"] == "waiting" and t["waiting_on"] == "question":
                evs = client.get(f"/api/tasks/{tid}/events", headers=h).json()
                text = [e for e in evs if e["kind"] == "question"][-1]["data"]["text"]
                questions += 1
                answer = ask_ai_human(text, timeout=120)
                assert client.post(f"/api/tasks/{tid}/respond", headers=h, json={"answer": answer}).status_code == 200
            elif t["status"] == "waiting" and t["waiting_on"] == "plan":
                assert client.post(f"/api/tasks/{tid}/respond", headers=h, json={"answer": "approve"}).status_code == 200
            else:
                time.sleep(0.5)
        t = wait_for(client, tid, lambda t: t["status"] in ("done", "failed", "cancelled"), 10, headers=h)
        assert t["status"] == "done", t.get("error")
        assert questions >= 1
