"""MCP tool implementations that delegate to the BS client.

Each tool function retrieves the current access token from the MCP auth context
(via AuthContextMiddleware) and creates a per-request BSClient with that token,
ensuring JWT passthrough from the OAuth flow to the BS backend.
"""

from __future__ import annotations

import json
import os

from mcp.server.auth.middleware.auth_context import get_access_token

from mcp_server.client import BSClient

# Module-level base URL for per-request client creation
_BS_BASE_URL = os.environ.get("BS_BASE_URL", "http://bs:8000")


def _get_client() -> BSClient:
    """Create a BSClient with the current request's access token.

    Retrieves the JWT from the MCP auth context (set by AuthContextMiddleware)
    and injects it as the Bearer token for BS API calls.
    """
    access_token = get_access_token()
    token = access_token.token if access_token else None
    return BSClient(base_url=_BS_BASE_URL, token=token, trust_env=False)


async def get_logs_tool(
    *,
    level: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    since: str | None = None,
    until: str | None = None,
) -> str:
    """Fetch logs from Bangumi-syncer. Returns JSON string."""
    client = _get_client()
    try:
        result = await client.get_logs(
            level=level,
            search=search,
            limit=limit,
            since=since,
            until=until,
        )
        return json.dumps(result, ensure_ascii=False)
    finally:
        await client.close()


async def get_current_config_tool() -> str:
    """Fetch current Bangumi-syncer config. Returns JSON string."""
    client = _get_client()
    try:
        result = await client.get_current_config()
        return json.dumps(result, ensure_ascii=False)
    finally:
        await client.close()


async def update_config_tool(
    *,
    section: str,
    key: str,
    value: str,
) -> str:
    """Update a Bangumi-syncer config value. Returns JSON string."""
    client = _get_client()
    try:
        result = await client.update_config(section=section, key=key, value=value)
        return json.dumps(result, ensure_ascii=False)
    finally:
        await client.close()
