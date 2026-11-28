"""Seam: the real superlite client against a live in-process slcloud, human orchestration end to end.

Each check is its own test with its own app. No network beyond 127.0.0.1; HOME/config are temp.
"""

import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from slcloud import app as slapp
from slcloud.worker import loop_once

from test_e2e import _setup

SCRIPTS = Path(os.environ.get("SUPERLITE_SCRIPTS", "/Users/iheatuwogu/Projects/supers/superlite-ho/skills/swarm/scripts"))
if not (SCRIPTS / "cloud_factory.py").exists():
    pytest.skip("superlite client scripts not found", allow_module_level=True)
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cloud  # noqa: E402
import cloud_client  # noqa: E402
import cloud_config  # noqa: E402
import cloud_factory  # noqa: E402
import cloud_watch  # noqa: E402
import factory  # noqa: E402
import factory_api  # noqa: E402
import factory_registry as reg  # noqa: E402

QUESTION = "Which login page: web or mobile?"


@pytest.fixture
def world(tmp_path, monkeypatch):
    for k in ("SUPERLITE_HOME", "CLAUDE_HOME"):
        (tmp_path / k).mkdir()
        monkeypatch.setenv(k, str(tmp_path / k))
    monkeypatch.setenv("HOME", str(tmp_path))
    app, st, remote, h, *_ = _setup(tmp_path, monkeypatch)
    srv = slapp._ProbeServer(app, tempfile.mkdtemp(prefix="slcloud-seam-"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://%s:%d" % srv.server_address
    for _ in range(100):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    token = h["Authorization"].split()[1]
    client = cloud_factory.CloudFactory(base, token, timeout=10)
    monkeypatch.setattr(cloud, "_client", lambda: (cloud_client.Client(base, token, timeout=10), {"token": token}))
    cloud_config.save({"url": base, "token": token})  # under the temp SUPERLITE_HOME
    try:
        yield SimpleNs(base=base, app=app, remote=remote, h=h, client=client)
    finally:
        srv.server_close()


class SimpleNs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def work(w):
    assert loop_once(w.app.state.worker_deps)


def runs_out(capsys):
    capsys.readouterr()
    assert cloud.cmd_runs(argparse.Namespace(all=False)) == 0
    return capsys.readouterr().out


def events(w, ref="login_fix"):
    """The plain JSON list: the client's SSE stream would block on a live run."""
    return httpx.get(f"{w.base}/api/tasks/{ref}/events", headers=w.h, timeout=10).json()


def send_human(w, prompt="ask fix the login bug"):
    return w.client.send(prompt, title="login_fix", repo=w.remote, base="main", orchestrator="human")


def test_check1_send_human_listed(world, capsys):
    run = send_human(world)
    assert run["name"] == "login_fix"
    line = next(ln for ln in runs_out(capsys).splitlines() if "login_fix" in ln)
    assert "[human]" in line


def test_check2_question_shown_as_waiting(world, capsys):
    send_human(world)
    work(world)
    line = next(ln for ln in runs_out(capsys).splitlines() if "login_fix" in ln)
    assert "[human]" in line and "waiting: question" in line
    ev = events(world)
    texts = [cloud_watch.render(e, color=False) for e in ev]
    assert "? " + QUESTION in [t for t in texts if t]


def test_check3_answer_via_watch_input_queues(world, capsys):
    send_human(world)
    work(world)
    state = cloud_watch.WatchState()
    for e in events(world):
        state.observe(e)
    assert state.get() == "question"
    cloud_watch.handle_input(world.client, "login_fix", "web\n", state)
    t = world.client.task("login_fix")
    assert t["status"] == "queued" and t["waiting_on"] is None
    work(world)
    line = next(ln for ln in runs_out(capsys).splitlines() if "login_fix" in ln)
    assert "waiting: plan" in line


def test_check4_plan_then_approve_done(world):
    send_human(world)
    work(world)
    cloud_watch.handle_input(world.client, "login_fix", "web", _state(world))
    work(world)
    t = world.client.task("login_fix")
    assert (t["status"], t["waiting_on"]) == ("waiting", "plan")
    cloud_watch.handle_input(world.client, "login_fix", "/approve")
    work(world)
    t = world.client.task("login_fix")
    assert t["status"] == "done" and t["result"]["pushed"] is True


def _state(w):
    s = cloud_watch.WatchState()
    for e in events(w):
        s.observe(e)
    return s


def test_check5_ai_send_never_parks_on_question(world, capsys):
    run = world.client.send("plan add x", title="tidy_readme", repo=world.remote, base="main")
    t = world.client.task(run["name"])
    assert t["orchestrator"] == "ai"
    work(world)
    t = world.client.task("tidy_readme")
    assert t["waiting_on"] == "plan"
    line = next(ln for ln in runs_out(capsys).splitlines() if "tidy_readme" in ln)
    assert "[human]" not in line and "waiting:" not in line
    # AI body carries no orchestrator key, and the preamble is the AI one
    assert factory_api.superlite_prompt("x").startswith(factory_api.SUPERLITE_PREAMBLE)
    assert factory_api.superlite_prompt("x", "human").startswith(factory_api.HUMAN_PREAMBLE)
    assert factory_api.HUMAN_PREAMBLE != factory_api.SUPERLITE_PREAMBLE


def test_check6_use_default_and_override(world, capsys):
    reg.use(reg.BUILTIN, orchestrator="human")
    assert reg.orchestrator_default(reg.BUILTIN) == "human"
    assert factory.main(["send", "plan add x", "--title", "dflt", "--repo", world.remote, "--base", "main"]) == 0
    assert world.client.task("dflt")["orchestrator"] == "human"
    assert factory.main(["send", "plan add y", "--title", "ovr", "--repo", world.remote, "--base", "main",
                         "--orchestrator", "ai"]) == 0
    assert world.client.task("ovr")["orchestrator"] == "ai"
    assert reg.orchestrator_default(reg.BUILTIN) == "human"  # the override is per send


def test_check7_server_rejects_bad_orchestrator(world):
    r = httpx.post(world.base + "/api/tasks", headers=world.h, timeout=10,
                   json={"repo": world.remote, "prompt": "p", "orchestrator": "robot"})
    assert r.status_code == 400
