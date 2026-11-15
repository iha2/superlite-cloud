"""Deploy files: compose.yaml, Caddyfile and Dockerfile.worker.

These tests read the files as text or YAML. They never run docker or compose.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def _worker_lines() -> list[str]:
    return (ROOT / "Dockerfile.worker").read_text().splitlines()


def test_port_local():
    server = _compose()["services"]["server"]
    assert server["ports"] == ["127.0.0.1:8787:8787"]


def test_caddy_profile():
    caddy = _compose()["services"]["caddy"]
    assert caddy["profiles"] == ["https"]
    assert "SLCLOUD_DOMAIN" in caddy["environment"]


def test_caddy_reverse_proxy():
    text = (ROOT / "Caddyfile").read_text()
    assert "{$SLCLOUD_DOMAIN}" in text
    assert "reverse_proxy server:8787" in text


def test_cursor_pin():
    lines = _worker_lines()
    assert "ARG CURSOR_VERSION=2026.11.13-eacade1" in lines
    text = "\n".join(lines)
    assert "cursor.com/install" not in text


def test_superlite_branch():
    lines = _worker_lines()
    assert "ARG SUPERLITE_VERSION=superlite/sessions/cloud-live-runs" in lines


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy is not installed")
def test_caddy_validate():
    env = dict(os.environ, SLCLOUD_DOMAIN="pat.example.com")
    result = subprocess.run(
        ["caddy", "validate", "--config", str(ROOT / "Caddyfile"), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
