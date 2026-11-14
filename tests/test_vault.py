import pytest

from slcloud.errors import Rejected
from slcloud.store import Store
from slcloud.vault import Vault, mask


def make():
    s = Store(":memory:")
    return s, Vault(s, "master")


def test_set_get_list():
    _, v = make()
    assert v.set("w1", "anthropic", "sk-ant-xyz1234") == "1234"
    assert v.get("w1", "anthropic") == "sk-ant-xyz1234"
    assert v.get("w1", "openai") is None
    assert v.list("w1") == [{"provider": "anthropic", "last4": "1234"}]


def test_ciphertext_not_plain():
    s, v = make()
    v.set("w1", "anthropic", "sk-ant-xyz1234")
    ct = s.query("SELECT ciphertext FROM keys")[0]["ciphertext"]
    assert "sk-ant-xyz1234" not in ct


def test_wrong_master_key():
    s, v = make()
    v.set("w1", "anthropic", "sk-ant-xyz1234")
    with pytest.raises(Rejected):
        Vault(s, "other").get("w1", "anthropic")


def test_other_workspace_none():
    _, v = make()
    v.set("w1", "anthropic", "sk-ant-xyz1234")
    assert v.get("w2", "anthropic") is None
    assert v.list("w2") == []


def test_mask():
    assert mask("err sk-ant-xyz1234 bad", ["sk-ant-xyz1234"]) == "err ***1234 bad"


def test_mask_empty_secret():
    assert mask("a", [""]) == "a"


def test_unknown_provider():
    _, v = make()
    with pytest.raises(ValueError):
        v.set("w1", "nope", "k")


def test_set_overwrites():
    _, v = make()
    v.set("w1", "openai", "aaaa1111")
    assert v.set("w1", "openai", "bbbb2222") == "2222"
    assert v.get("w1", "openai") == "bbbb2222"
    assert len(v.list("w1")) == 1


def test_empty_master_key_rejected():
    with pytest.raises(Rejected):
        Vault(Store(":memory:"), "")


def test_mask_overlapping_and_repeated():
    out = mask("x abcd1234 y 1234 z abcd1234", ["1234", "abcd1234"])
    assert "abcd" not in out and out.count("***") == 3


def test_mask_short_secret_not_revealed():
    assert mask("tok=abc", ["abc"]) == "tok=***"


def test_wrong_master_error_has_no_plaintext():
    s, v = make()
    v.set("w1", "anthropic", "sk-ant-xyz1234")
    with pytest.raises(Rejected) as e:
        Vault(s, "other").get("w1", "anthropic")
    assert "sk-ant" not in str(e.value) and "sk-ant" not in repr(e.value)
    assert "sk-ant" not in repr(v)


def test_nonce_differs_per_encrypt():
    s, v = make()
    v.set("w1", "openai", "aaaa1111")
    a = s.query("SELECT ciphertext FROM keys")[0]["ciphertext"]
    v.set("w1", "openai", "aaaa1111")
    assert a != s.query("SELECT ciphertext FROM keys")[0]["ciphertext"]
