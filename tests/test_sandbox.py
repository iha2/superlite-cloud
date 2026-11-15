import subprocess

import pytest

from slcloud.sandbox import DockerRunner, Job, JobResult, ProcessRunner, get_runner


def job(**kw):
    base = dict(image="img", command=["echo", "hi"], env={"ANTHROPIC_API_KEY": "sekret"}, workdir="/w")
    base.update(kw)
    return Job(**base)


def test_process_run():
    r = ProcessRunner().run(Job("x", ["sh", "-c", "echo hi"], {}, "/tmp"))
    assert r == JobResult(code=0, stdout="hi\n", stderr="", timed_out=False)


def test_timeout():
    r = ProcessRunner().run(Job("x", ["sleep", "5"], {}, "/tmp", timeout_s=1))
    assert r.timed_out is True and r.code == -1


def test_process_env_minimal():
    r = ProcessRunner().run(Job("x", ["env"], {"FOO": "bar"}, "/tmp"))
    names = {l.split("=", 1)[0] for l in r.stdout.splitlines() if "=" in l}
    assert {"PATH", "HOME", "FOO"} <= names
    assert names <= {"PATH", "HOME", "FOO", "PWD", "SHLVL", "_", "OLDPWD"}


def test_argv_shape(monkeypatch):
    monkeypatch.delenv("SLCLOUD_DOCKER_BIN", raising=False)
    argv = DockerRunner().argv(job())
    assert argv[:17] == ["docker", "run", "--rm", "--cpus", "2.0", "--memory", "4g",
                         "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt",
                         "no-new-privileges", "-v", "/w:/work", "-w", "/work"]
    assert argv[17:] == ["-e", "ANTHROPIC_API_KEY", "img", "echo", "hi"]


def test_gvisor_runtime():
    assert "--runtime" in DockerRunner(gvisor=True).argv(job())
    a = DockerRunner(gvisor=True).argv(job())
    assert a[a.index("--runtime") + 1] == "runsc"
    assert "--runtime" not in DockerRunner().argv(job())


def test_env_values_not_in_argv():
    calls = []

    def fake(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "out", "err")

    r = DockerRunner(run=fake).run(job())
    argv, kw = calls[0]
    assert "sekret" not in " ".join(argv)
    assert argv[2] == "--name" and argv[3].startswith("slcloud-")
    assert kw["env"]["ANTHROPIC_API_KEY"] == "sekret"
    assert kw["timeout"] == 3600
    assert r == JobResult(0, "out", "err", False)


def test_docker_timeout():
    def fake(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1, output=b"o", stderr=None)

    r = DockerRunner(run=fake).run(job(timeout_s=1))
    assert r.timed_out is True and r.code == -1 and r.stdout == "o"


def test_no_docker_sock():
    assert "docker.sock" not in " ".join(DockerRunner(gvisor=True).argv(job()))


def test_get_runner():
    assert isinstance(get_runner("process"), ProcessRunner)
    d = get_runner("docker", gvisor=True)
    assert isinstance(d, DockerRunner) and d.gvisor is True
    with pytest.raises(ValueError):
        get_runner("vm")


def test_timeout_removes_container(monkeypatch):
    monkeypatch.delenv("SLCLOUD_DOCKER_BIN", raising=False)
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    r = DockerRunner(run=fake).run(job(workdir="/w", timeout_s=1))
    assert r.timed_out
    assert calls[1] == ["docker", "rm", "-f", calls[0][3]]


@pytest.mark.parametrize("kw", [
    {"image": "--privileged"}, {"workdir": "/w:/etc"}, {"workdir": "rel"},
    {"env": {"A=b": "x"}}, {"env": {"-v": "x"}},
])
def test_rejects_injection(kw):
    with pytest.raises(ValueError):
        DockerRunner().argv(job(**kw))


def _capture():
    calls = []

    def fake(argv, **kw):
        calls.append((argv, kw))
        if argv[1] == "run" and kw.get("timeout") == 1:
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return calls, fake


def test_docker_env_passthrough(monkeypatch):
    for k in ("DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DOCKER_HOST", "unix:///x/podman.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "ctx")
    monkeypatch.setenv("SOME_OTHER_SECRET", "nope")
    calls, fake = _capture()
    DockerRunner(run=fake).run(job(timeout_s=1))
    run_env, rm_env = calls[0][1]["env"], calls[1][1]["env"]
    assert calls[1][0][1:3] == ["rm", "-f"]
    for env in (run_env, rm_env):
        assert env["DOCKER_HOST"] == "unix:///x/podman.sock" and env["DOCKER_CONTEXT"] == "ctx"
        assert "DOCKER_CONFIG" not in env and "SOME_OTHER_SECRET" not in env
    assert run_env["ANTHROPIC_API_KEY"] == "sekret"
    assert "ANTHROPIC_API_KEY" not in rm_env


def test_docker_bin_from_env(monkeypatch):
    monkeypatch.delenv("SLCLOUD_DOCKER_BIN", raising=False)
    assert DockerRunner().argv(job())[0] == "docker"
    monkeypatch.setenv("SLCLOUD_DOCKER_BIN", "podman")
    assert DockerRunner().argv(job())[0] == "podman"
    assert get_runner("docker").docker_bin == "podman"
    calls, fake = _capture()
    DockerRunner(run=fake).run(job(timeout_s=1))
    assert calls[0][0][0] == "podman" and calls[1][0][:3] == ["podman", "rm", "-f"]
    assert DockerRunner(docker_bin="nerdctl").argv(job())[0] == "nerdctl"


def test_docker_runner_uses_env_bin_when_no_arg(monkeypatch):
    monkeypatch.setenv("SLCLOUD_DOCKER_BIN", "/opt/bin/podman")
    r = DockerRunner()
    assert r.docker_bin == "/opt/bin/podman"
    assert r.argv(job())[0] == "/opt/bin/podman"


# --- long-lived start() ---
import subprocess as _sp
import sys

from slcloud.sandbox import Mount


def _fake_popen(cmd=None, calls=None):
    def popen(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        real = cmd or [sys.executable, "-c", "import sys;[print(l.upper(),end='') for l in sys.stdin]"]
        return _sp.Popen(real, **kw)
    return popen


def test_start_stdin_open():
    calls = []
    p = DockerRunner(popen=_fake_popen(calls=calls)).start(job())
    p.write("hi\n")
    p.close_stdin()
    assert list(p.lines()) == ["HI"]
    p.wait()
    argv, kw = calls[0]
    assert argv[argv.index("run") + 1] == "-i"
    assert kw["stdin"] == _sp.PIPE


def test_timeout_kills():
    calls, rm = [], []

    def run(argv, **kw):
        rm.append(argv)
        return _sp.CompletedProcess(argv, 0, "", "")

    d = DockerRunner(run=run, popen=_fake_popen([ "sleep", "5"], calls))
    r = d.start(job(timeout_s=1)).wait()
    assert r.timed_out is True
    assert rm and rm[0][1:3] == ["rm", "-f"] and rm[0][3] == calls[0][0][4]


def test_mount_validation():
    for bad in (Mount("/a:b", "/c"), Mount("/a", "/c,d"), Mount("/a", "/c:ro")):
        with pytest.raises(ValueError):
            DockerRunner().argv(job(mounts=[bad]))


def test_mount_argv():
    a = DockerRunner().argv(job(mounts=[Mount("/d/x.git", "/d/x.git"), Mount("/h", "/p", False)]))
    i = a.index("/d/x.git:/d/x.git:ro")
    assert a[i - 1] == "-v" and a[a.index("/h:/p") - 1] == "-v"


def test_env_not_in_argv():
    calls = []
    p = DockerRunner(popen=_fake_popen(calls=calls)).start(job())
    p.close_stdin()
    p.wait()
    argv, kw = calls[0]
    assert "sekret" not in " ".join(argv)
    assert kw["env"]["ANTHROPIC_API_KEY"] == "sekret"


def test_process_start_roundtrip():
    p = ProcessRunner().start(Job("x", [sys.executable, "-c",
                              "import sys;[print(l.upper(),end='') for l in sys.stdin]"], {}, "/tmp"))
    p.write("hi\n")
    p.close_stdin()
    assert list(p.lines()) == ["HI"]
    assert p.wait().code == 0


def test_kill_removes_container():
    """Regression: Proc.kill() only killed the local docker client, leaving the container."""
    calls, rm = [], []

    def run(argv, **kw):
        rm.append(argv)
        return _sp.CompletedProcess(argv, 0, "", "")

    d = DockerRunner(run=run, popen=_fake_popen(["sleep", "5"], calls))
    p = d.start(job(timeout_s=100))
    p.kill()
    p.wait()
    assert rm and rm[0][1:3] == ["rm", "-f"] and rm[0][3] == calls[0][0][4]
