"""Shared bare repo cache per workspace and repo."""
from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager

_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor="]


def _git(args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {p.stderr.strip()[:300]}")
    return p


class RepoCache:
    _tlocks: dict[str, threading.Lock] = {}
    _tlocks_guard = threading.Lock()

    def __init__(self, path: str, repo: str):
        self.path = path
        self.repo = repo

    def _ensure(self) -> None:
        if os.path.isdir(os.path.join(self.path, "objects")):
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        _git(["init", "--bare", "-q", self.path])
        _git(["-C", self.path, "remote", "add", "origin", self.repo])

    @contextmanager
    def lock(self):
        with RepoCache._tlocks_guard:
            tl = RepoCache._tlocks.setdefault(self.path, threading.Lock())
        with tl:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".lock", "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def _touch(self) -> None:
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def fetch(self) -> set[str]:
        self._ensure()
        _git(["-C", self.path, *_SAFE, "fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*"])
        self._touch()
        out = _git(["-C", self.path, "for-each-ref", "--format=%(refname:short)", "refs/heads"]).stdout
        return {ln for ln in out.splitlines() if ln}

    def light_clone(self, base: str, dest: str) -> None:
        self._ensure()
        _git([*_SAFE, "clone", "--shared", "--no-checkout", "-q", self.path, dest])
        _git([*_SAFE, "-C", dest, "checkout", "-q", base])
        self._touch()

    def fetch_from(self, src: str, branch: str, into: str) -> str:
        self._ensure()
        _git(["-C", self.path, *_SAFE, "fetch", "--no-tags", src, f"+refs/heads/{branch}:{into}"])
        return _git(["-C", self.path, "rev-parse", into]).stdout.strip()

    def push(self, ref: str, branch: str) -> bool:
        p = _git(["-C", self.path, *_SAFE, "push", "origin", f"{ref}:refs/heads/{branch}"], check=False)
        return p.returncode == 0

    def bundle(self, ref: str, out: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        _git(["-C", self.path, "bundle", "create", out, ref])
        return out

    def drop_ref(self, ref: str) -> None:
        _git(["-C", self.path, "update-ref", "-d", ref], check=False)


def cache_for(data_dir: str, workspace: str, repo: str) -> RepoCache:
    h = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return RepoCache(os.path.join(data_dir, "repos", workspace, h + ".git"), repo)


def cleanup(data_dir: str, in_use: set[str], now: float, idle_days: int = 14) -> list[str]:
    root = os.path.join(data_dir, "repos")
    removed: list[str] = []
    if not os.path.isdir(root):
        return removed
    cutoff = now - idle_days * 86400
    for ws in sorted(os.listdir(root)):
        wdir = os.path.join(root, ws)
        if not os.path.isdir(wdir):
            continue
        for ent in sorted(os.listdir(wdir)):
            p = os.path.join(wdir, ent)
            if not ent.endswith(".git") or not os.path.isdir(p) or p in in_use:
                continue
            if os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                try:
                    os.remove(p + ".lock")
                except OSError:
                    pass
                removed.append(p)
    return removed
