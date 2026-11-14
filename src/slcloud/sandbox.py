"""Sandboxed job runners: local process (dev) and docker (optionally gVisor)."""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass


@dataclass
class Job:
    image: str
    command: list[str]
    env: dict[str, str]
    workdir: str
    timeout_s: int = 3600
    cpus: float = 2.0
    memory: str = "4g"


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


# Client settings the container CLI needs to find its daemon. Never job.env secrets.
_DOCKER_PASSTHROUGH = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                       "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")


class DockerRunner:
    def __init__(self, gvisor: bool = False, run=subprocess.run, docker_bin: str | None = None):
        self.gvisor = gvisor
        self._run = run
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
        for k in job.env:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                raise ValueError(f"invalid env name: {k!r}")
        a = [self.docker_bin, "run", "--rm", "--cpus", str(job.cpus), "--memory", job.memory,
             "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        if self.gvisor:
            a += ["--runtime", "runsc"]
        a += ["-v", f"{job.workdir}:/work", "-w", "/work"]
        for k in job.env:
            a += ["-e", k]
        return a + [job.image, *job.command]

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


def get_runner(kind: str, gvisor: bool = False):
    if kind == "process":
        return ProcessRunner()
    if kind == "docker":
        return DockerRunner(gvisor=gvisor)
    raise ValueError(f"unknown sandbox kind: {kind}")
