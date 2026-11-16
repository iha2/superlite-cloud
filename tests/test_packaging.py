import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT_FILES = ["Dockerfile", "Dockerfile.worker", "compose.yaml", ".dockerignore", ".env.example"]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_compose_services():
    services = _compose()["services"]
    assert "server" in services
    assert "worker" in services
    assert "127.0.0.1:8787:8787" in services["server"]["ports"]
    binds = {n: [str(v) for v in services[n]["volumes"] if "SLCLOUD_DATA" in str(v)]
             for n in ("server", "worker")}
    for mounts in binds.values():  # host path == container path, so sandboxes can bind it
        (src, dst), = [m.rsplit(":${", 1) for m in mounts]
        assert src.startswith("${SLCLOUD_DATA") and "${" + dst == src
    assert "slcloud-data" not in _compose().get("volumes", {})
    assert " ".join(services["worker"]["command"]) == "slcloud worker"


def test_pinned():
    text = (ROOT / "Dockerfile.worker").read_text()
    for name in ["CLAUDE_VERSION", "CODEX_VERSION", "GEMINI_VERSION", "SUPERLITE_VERSION"]:
        match = re.search(rf"^ARG {name}=(\S+)$", text, re.MULTILINE)
        assert match is not None, name
        assert match.group(1), name
    for name in TEXT_FILES:
        assert "latest" not in (ROOT / name).read_text().lower(), name


def test_sandbox_image_no_sock():
    services = _compose()["services"]
    mounting = [
        name
        for name, svc in services.items()
        if any("/var/run/docker.sock" in str(v) for v in svc.get("volumes", []))
    ]
    assert mounting == ["worker"]
    assert "docker.sock" not in (ROOT / "Dockerfile.worker").read_text()
