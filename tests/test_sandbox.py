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


def test_argv_shape():
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


def test_timeout_removes_container():
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
