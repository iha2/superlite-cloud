import pytest

from slcloud import names


@pytest.mark.parametrize("task,want", [
    ("Fix the login redirect", "login_fix"),
    ("Dark mode for settings page", "dark_mode"),
    ("", "run"),
    ("!!!", "run"),
    ("fix", "fix"),
    ("Add dark mode", "dark_add"),
])
def test_make_name(task, want):
    assert names.make_name(task) == want
    names.check_name(names.make_name(task))


@pytest.mark.parametrize("bad", ["a_b_c_d", "Login-Fix", "", "_a", "a__b", "a b"])
def test_check_name_rejects(bad):
    with pytest.raises(ValueError):
        names.check_name(bad)


def test_check_name_ok():
    assert names.check_name("login_fix") == "login_fix"


def test_slug():
    assert names.slug("login_fix") == "login-fix"


def test_free_name():
    assert names.free_name("auth", set()) == "auth"
    assert names.free_name("auth", {"auth"}) == "auth_2"
    assert names.free_name("auth", {"auth", "auth_2"}) == "auth_3"
