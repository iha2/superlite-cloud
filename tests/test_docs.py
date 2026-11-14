from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_commands():
    text = (ROOT / "README.md").read_text()
    for command in [
        "docker compose up",
        "slcloud init",
        "slcloud key set",
        "slcloud connect",
        "slcloud sync",
    ]:
        assert command in text, command


def test_readme_token_create_before_connect():
    text = (ROOT / "README.md").read_text()
    assert "slcloud token create <workspace> <role>" in text
    assert text.index("slcloud token create") < text.index("slcloud connect")


def test_master_key_generation_documented():
    assert "openssl rand -hex 32" in (ROOT / "SECURITY.md").read_text()
    assert "openssl rand -hex 32" in (ROOT / ".env.example").read_text()


def test_license_mit():
    text = (ROOT / "LICENSE").read_text()
    assert "MIT License" in text
    assert "Permission is hereby granted, free of charge" in text


def test_security_mentions():
    text = (ROOT / "SECURITY.md").read_text()
    assert "--bare" in text
    assert "docker.sock" in text
    assert "API key" in text
