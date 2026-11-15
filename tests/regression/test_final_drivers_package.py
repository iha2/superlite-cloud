import subprocess
import sys


def test_import_exposes_submodules_and_classes():
    code = (
        "import slcloud.drivers as d\n"
        "assert d.base.Driver.__name__ == 'Driver'\n"
        "for mod, cls in (('claude','ClaudeDriver'),('codex','CodexDriver'),('cursor','CursorDriver'),('gemini','GeminiDriver')):\n"
        "    c = getattr(getattr(d, mod), cls)\n"
        "    assert getattr(d, cls) is c and issubclass(c, d.base.Driver), cls\n"
        "assert d.get_driver('codex', lambda a: None).__class__ is d.CodexDriver\n"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_unknown_agent():
    import pytest

    from slcloud.drivers import get_driver
    with pytest.raises(ValueError):
        get_driver("nope", lambda a: None)
