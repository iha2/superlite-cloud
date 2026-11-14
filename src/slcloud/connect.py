"""MCP connect command text for each agent."""

from __future__ import annotations

import json

from slcloud.errors import Rejected

AGENTS = ("claude", "codex", "gemini", "cursor")


def connect_command(agent: str, url: str, token: str) -> str:
    mcp = f"{url.rstrip('/')}/mcp"
    header = f'"Authorization: Bearer {token}"'
    if agent == "claude":
        return f"claude mcp add --transport http slcloud {mcp} --header {header}"
    if agent == "gemini":
        return f"gemini mcp add --transport http slcloud {mcp} --header {header}"
    if agent == "codex":
        return (
            f"export SLCLOUD_TOKEN={token} && codex mcp add slcloud --url {mcp} "
            "--bearer-token-env-var SLCLOUD_TOKEN"
        )
    if agent == "cursor":
        return json.dumps(
            {"mcpServers": {"slcloud": {"url": mcp, "headers": {"Authorization": f"Bearer {token}"}}}},
            indent=2,
        )
    raise Rejected(f"unknown agent: {agent}")
