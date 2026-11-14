"""Provider key check. HTTP is injected; no secret is ever returned."""

from __future__ import annotations

import httpx

_ENDPOINTS = {
    "anthropic": (
        "https://api.anthropic.com/v1/models",
        lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"},
    ),
    "openai": (
        "https://api.openai.com/v1/models",
        lambda k: {"Authorization": f"Bearer {k}"},
    ),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/models",
        lambda k: {"x-goog-api-key": k},
    ),
}


def test_key(provider: str, key: str, get=httpx.get) -> tuple[bool, str]:
    if provider not in _ENDPOINTS:
        return True, "not checked"
    url, headers = _ENDPOINTS[provider]
    try:
        resp = get(url, headers=headers(key), timeout=10)
    except Exception as exc:  # network failure; never echo the key
        return False, f"request failed: {type(exc).__name__}"
    if resp.status_code == 200:
        return True, "works"
    msg = f"HTTP {resp.status_code}"
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            err = err.get("message")
        if isinstance(err, str) and err:
            msg = err
    except Exception:
        pass
    return False, msg.replace(key, "***") if key else msg


test_key.__test__ = False  # not a pytest test
