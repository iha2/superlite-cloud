"""Provider key vault."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .errors import Rejected
from .store import Store

PROVIDERS = ("anthropic", "openai", "gemini", "cursor")


class Vault:
    """Per-workspace provider key store, encrypted with the master key."""

    def __init__(self, store: Store, master_key: str):
        if not master_key:
            raise Rejected("master key is empty")
        self._store = store
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(master_key.encode()).digest()))

    def set(self, workspace: str, provider: str, key: str) -> str:
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider: {provider}")
        last4 = _tail(key)
        ct = self._fernet.encrypt(key.encode()).decode()
        self._store.exec(
            "INSERT INTO keys(workspace,provider,ciphertext,last4) VALUES(?,?,?,?) "
            "ON CONFLICT(workspace,provider) DO UPDATE SET ciphertext=excluded.ciphertext, last4=excluded.last4",
            (workspace, provider, ct, last4),
        )
        return last4

    def get(self, workspace: str, provider: str) -> str | None:
        rows = self._store.query(
            "SELECT ciphertext FROM keys WHERE workspace=? AND provider=?", (workspace, provider)
        )
        if not rows:
            return None
        try:
            return self._fernet.decrypt(rows[0]["ciphertext"].encode()).decode()
        except InvalidToken:
            raise Rejected("cannot decrypt key: wrong master key") from None

    def list(self, workspace: str) -> list[dict]:
        return self._store.query(
            "SELECT provider, last4 FROM keys WHERE workspace=? ORDER BY provider", (workspace,)
        )


def _tail(s: str) -> str:
    # Never reveal a whole short secret.
    return s[-4:] if len(s) > 4 else ""


def mask(text: str, secrets: list[str]) -> str:
    """Single-pass masking so overlapping secrets and repeated hits cannot re-mask output."""
    uniq = sorted({s for s in secrets if s}, key=len, reverse=True)
    if not uniq:
        return text
    import re

    pat = re.compile("|".join(re.escape(s) for s in uniq))
    return pat.sub(lambda m: "***" + _tail(m.group(0)), text)
