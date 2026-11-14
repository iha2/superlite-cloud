"""Webhook signing and task notifications."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from .store import Store


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Notifier:
    """Posts a signed task snapshot to each webhook of the task's workspace."""

    def __init__(self, store: Store, post=httpx.post):
        self._store = store
        self._post = post

    def __call__(self, task) -> None:
        ws = getattr(task, "workspace", None) or getattr(task, "ws", None)
        try:
            hooks = self._store.query("SELECT url, secret FROM webhooks WHERE workspace=?", (ws,))
        except Exception:
            return
        body = json.dumps({"task": task.public()}, sort_keys=True).encode()
        for h in hooks:
            try:
                self._post(
                    h["url"],
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Slcloud-Signature": sign(h["secret"] or "", body),
                    },
                    timeout=10,
                )
            except Exception:
                continue
