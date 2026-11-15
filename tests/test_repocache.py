import os
import subprocess
import threading
import time

import pytest

from slcloud import repocache


def git(*a, cwd=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.com")
    return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    o = tmp_path / "origin"
    git("init", "-q", "-b", "main", str(o))
    (o / "a.txt").write_text("1")
    git("add", ".", cwd=o)
    git("commit", "-qm", "one", cwd=o)
    git("branch", "feature", cwd=o)
    return str(o)


@pytest.fixture
def cache(tmp_path, origin):
    c = repocache.cache_for(str(tmp_path / "data"), "w_ab12", origin)
    c.fetch()
    return c


def test_cache_path(tmp_path):
    import hashlib
    c = repocache.cache_for("/data", "w_ab12", "https://github.com/acme/app")
    h = hashlib.sha256(b"https://github.com/acme/app").hexdigest()[:16]
    assert c.path == f"/data/repos/w_ab12/{h}.git"


def test_fetch_branches(cache):
    assert cache.fetch() == {"main", "feature"}


def test_no_working_files(cache):
    assert git("rev-parse", "--is-bare-repository", cwd=cache.path) == "true"
    assert not os.path.exists(os.path.join(cache.path, "a.txt"))


def test_light_clone_shared(cache, tmp_path):
    dest = str(tmp_path / "run")
    cache.light_clone("main", dest)
    alt = open(os.path.join(dest, ".git/objects/info/alternates")).read().strip()
    assert os.path.realpath(alt) == os.path.realpath(os.path.join(cache.path, "objects"))
    assert (tmp_path / "run" / "a.txt").read_text() == "1"


def _commit_in(run, name="b.txt"):
    (run / name).write_text("2")
    git("add", ".", cwd=run)
    git("commit", "-qm", "two", cwd=run)
    git("checkout", "-qb", "superlite/sessions/login-fix", cwd=run)


def test_fetch_from(cache, tmp_path):
    run = tmp_path / "run"
    cache.light_clone("main", str(run))
    _commit_in(run)
    sha = cache.fetch_from(str(run), "superlite/sessions/login-fix", "refs/runs/t_1")
    assert len(sha) == 40 and sha == git("rev-parse", "HEAD", cwd=run)
    cache.drop_ref("refs/runs/t_1")
    assert subprocess.run(["git", "-C", cache.path, "rev-parse", "--verify", "-q", "refs/runs/t_1"],
                          capture_output=True).returncode != 0


def test_planted_hook_not_run(cache, tmp_path):
    run = tmp_path / "run"
    cache.light_clone("main", str(run))
    _commit_in(run)
    marker = tmp_path / "pwned"
    script = run / ".git" / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    for h in ("post-receive", "pre-receive", "upload-pack", "reference-transaction"):
        p = run / ".git" / "hooks" / h
        p.write_text(f"#!/bin/sh\ntouch {marker}\n")
        p.chmod(0o755)
    git("config", "core.fsmonitor", str(script), cwd=run)
    git("config", "uploadpack.packObjectsHook", str(script), cwd=run)
    cache.fetch_from(str(run), "superlite/sessions/login-fix", "refs/runs/t_1")
    assert not marker.exists()


def test_push_and_refused(cache, tmp_path, origin):
    run = tmp_path / "run"
    cache.light_clone("main", str(run))
    _commit_in(run)
    cache.fetch_from(str(run), "superlite/sessions/login-fix", "refs/runs/t_1")
    # origin is non-bare with main checked out; push to a new branch name works
    assert cache.push("refs/runs/t_1", "superlite/sessions/login-fix") is True
    # diverge: a different commit onto the same branch is non-fast-forward
    other = tmp_path / "other"
    cache.light_clone("main", str(other))
    (other / "c.txt").write_text("3")
    git("add", ".", cwd=other)
    git("commit", "-qm", "three", cwd=other)
    git("checkout", "-qb", "x", cwd=other)
    cache.fetch_from(str(other), "x", "refs/runs/t_2")
    assert cache.push("refs/runs/t_2", "superlite/sessions/login-fix") is False


def test_bundle(cache, tmp_path):
    out = cache.bundle("main", str(tmp_path / "b" / "main.bundle"))
    assert os.path.getsize(out) > 0


def test_cleanup_idle(tmp_path):
    d = str(tmp_path / "data")
    old = repocache.cache_for(d, "w_ab12", "r1")
    keep = repocache.cache_for(d, "w_ab12", "r2")
    fresh = repocache.cache_for(d, "w_ab12", "r3")
    now = time.time()
    for c in (old, keep, fresh):
        os.makedirs(c.path)
    for c in (old, keep):
        os.utime(c.path, (now - 15 * 86400, now - 15 * 86400))
    assert repocache.cleanup(d, {keep.path}, now) == [old.path]
    assert not os.path.exists(old.path) and os.path.exists(keep.path) and os.path.exists(fresh.path)


def test_lock_exclusive(cache):
    got = []
    with cache.lock():
        def other():
            with cache.lock():
                got.append(1)
        t = threading.Thread(target=other)
        t.start()
        t.join(0.3)
        assert got == [] and t.is_alive()
    t.join(2)
    assert got == [1]
