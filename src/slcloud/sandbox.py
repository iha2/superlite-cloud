"""Sandboxed job runners: local process (dev) and docker (optionally gVisor)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field


@dataclass
class Mount:
    host: str
    path: str
    read_only: bool = True


@dataclass
class Job:
    image: str
    command: list[str]
    env: dict[str, str]
    workdir: str
    timeout_s: int = 3600
    cpus: float = 2.0
    memory: str = "4g"
    mounts: list[Mount] = field(default_factory=list)


@dataclass
class JobResult:
    code: int
    stdout: str
    stderr: str
    timed_out: bool


def _text(v) -> str:
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


class Proc:
    """A running job with open stdin and line-buffered stdout."""

    def __init__(self, popen, timeout_s: int | None = None,
                 on_timeout: Callable[[], None] | None = None,
                 on_kill: Callable[[], None] | None = None):
        self._p = popen
        self._on_kill = on_kill
        self._err: list[str] = []
        self.timed_out = False
        self._on_timeout = on_timeout
        self._timer = None
        if getattr(popen, "stderr", None) is not None:
            self._drain = threading.Thread(target=self._read_err, daemon=True)
            self._drain.start()
        else:
            self._drain = None
        if timeout_s:
            self._timer = threading.Timer(timeout_s, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def _read_err(self) -> None:
        try:
            for chunk in self._p.stderr:
                self._err.append(_text(chunk))
        except (ValueError, OSError):
            pass

    def _expire(self) -> None:
        self.timed_out = True
        self.kill()
        if self._on_timeout:
            try:
                self._on_timeout()
            except Exception:
                pass

    def write(self, text: str) -> None:
        self._p.stdin.write(text)
        self._p.stdin.flush()

    def lines(self) -> Iterator[str]:
        for line in self._p.stdout:
            yield _text(line).rstrip("\r\n")

    def close_stdin(self) -> None:
        try:
            self._p.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def wait(self) -> JobResult:
        code = self._p.wait()
        if self._timer:
            self._timer.cancel()
        if self._drain:
            self._drain.join(2)
        return JobResult(-1 if self.timed_out else code, "", "".join(self._err), self.timed_out)

    def kill(self) -> None:
        try:
            self._p.kill()
        except (ProcessLookupError, OSError):
            pass
        if self._on_kill:
            try:
                self._on_kill()
            except Exception:
                pass


def _spawn(popen, argv, **kw):
    return popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, bufsize=1, **kw)


class ProcessRunner:
    """Runs a job as a local process with a minimal environment."""

    def run(self, job: Job) -> JobResult:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": job.workdir, **job.env}
        try:
            p = subprocess.run(job.command, cwd=job.workdir, env=env, capture_output=True,
                               text=True, timeout=job.timeout_s)
        except subprocess.TimeoutExpired as e:
            return JobResult(-1, _text(e.stdout), _text(e.stderr), True)
        return JobResult(p.returncode, p.stdout, p.stderr, False)


    def start(self, job: Job, popen=subprocess.Popen) -> Proc:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": job.workdir, **job.env}
        return Proc(_spawn(popen, job.command, cwd=job.workdir, env=env), job.timeout_s)


# Client settings the container CLI needs to find its daemon. Never job.env secrets.
_DOCKER_PASSTHROUGH = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                       "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")


class DockerRunner:
    def __init__(self, gvisor: bool = False, run=subprocess.run, docker_bin: str | None = None,
                 popen=subprocess.Popen):
        self.gvisor = gvisor
        self._run = run
        self._popen = popen
        self.docker_bin = docker_bin or os.environ.get("SLCLOUD_DOCKER_BIN") or "docker"

    def _cli_env(self) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": os.environ.get("HOME", "/")}
        for k in _DOCKER_PASSTHROUGH:
            if k in os.environ:
                env[k] = os.environ[k]
        return env

    def argv(self, job: Job) -> list[str]:
        if job.image.startswith("-") or not job.image:
            raise ValueError("invalid image")
        if ":" in job.workdir or "," in job.workdir or not os.path.isabs(job.workdir):
            raise ValueError("invalid workdir")
        for m in job.mounts:
            for v in (m.host, m.path):
                if ":" in v or "," in v or not os.path.isabs(v):
                    raise ValueError("invalid mount")
        for k in job.env:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                raise ValueError(f"invalid env name: {k!r}")
        a = [self.docker_bin, "run", "--rm", "--cpus", str(job.cpus), "--memory", job.memory,
             "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        if self.gvisor:
            a += ["--runtime", "runsc"]
        a += ["-v", f"{job.workdir}:/work", "-w", "/work"]
        for m in [*job.mounts, *state_mounts(job.workdir)]:
            a += ["-v", f"{m.host}:{m.path}" + (":ro" if m.read_only else "")]
        for k in job.env:
            a += ["-e", k]
        return a + [job.image, *job.command]

    def start(self, job: Job) -> Proc:
        cli_env = self._cli_env()
        name = f"slcloud-{uuid.uuid4().hex[:12]}"
        argv = self.argv(job)
        argv[2:2] = ["-i", "--name", name]

        def rm() -> None:
            self._run([self.docker_bin, "rm", "-f", name], env=cli_env, capture_output=True,
                      text=True, timeout=30)

        return Proc(_spawn(self._popen, argv, env={**cli_env, **job.env}), job.timeout_s, rm, on_kill=rm)

    def run(self, job: Job) -> JobResult:
        cli_env = self._cli_env()
        env = {**cli_env, **job.env}
        name = f"slcloud-{uuid.uuid4().hex[:12]}"
        argv = self.argv(job)
        argv[2:2] = ["--name", name]
        try:
            p = self._run(argv, env=env, capture_output=True, text=True,
                          timeout=job.timeout_s)
        except subprocess.TimeoutExpired as e:
            try:
                self._run([self.docker_bin, "rm", "-f", name], env=cli_env, capture_output=True,
                          text=True, timeout=30)
            except Exception:
                pass
            return JobResult(-1, _text(e.stdout), _text(e.stderr), True)
        return JobResult(p.returncode, _text(p.stdout), _text(p.stderr), False)


# Agent session state that must survive between the per-turn containers of one run.
_STATE_DIRS = {"claude": "/root/.claude", "codex": "/root/.codex", "gemini": "/root/.gemini",
               "cursor": "/root/.cursor", "cursor-config": "/root/.config/cursor"}


def state_root(workdir: str) -> str:
    """Worker-controlled home of a run's agent state: beside the checkout, never inside it,
    because the agent can write anywhere in the checkout (including by planting symlinks)."""
    workdir = os.path.abspath(workdir)
    return os.path.join(os.path.dirname(workdir), ".slcloud-agent", os.path.basename(workdir))


def _safe_dir(path: str) -> None:
    """Create path if missing; refuse anything already there that is not a real directory."""
    try:
        os.mkdir(path)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(os.lstat(path).st_mode):  # lstat: a symlink is not a directory here
        raise ValueError(f"unsafe agent state path: {path}")


def remove_state(workdir: str) -> None:
    """Drop a run's agent state; call wherever the run directory is removed."""
    shutil.rmtree(state_root(workdir), ignore_errors=True)


def state_mounts(workdir: str) -> list[Mount]:
    """Per-run agent state, kept outside the checkout so it is never committed and cannot be
    redirected by the agent. Empty when the workdir is not a git checkout."""
    if not os.path.isdir(os.path.join(workdir, ".git")):
        return []
    root = state_root(workdir)
    _safe_dir(os.path.dirname(root))
    _safe_dir(root)
    out = []
    for name, target in _STATE_DIRS.items():
        host = os.path.join(root, name)
        _safe_dir(host)
        out.append(Mount(host, target, False))
    return out


def get_runner(kind: str, gvisor: bool = False):
    if kind == "process":
        return ProcessRunner()
    if kind == "docker":
        return DockerRunner(gvisor=gvisor)
    raise ValueError(f"unknown sandbox kind: {kind}")
