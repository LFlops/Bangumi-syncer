"""HTTP client for calling BS internal API (/api/mcp/*)."""

from __future__ import annotations

import os
from typing import Any

import httpx


class BSAPIError(Exception):
    """Raised when BS returns a non-success envelope or client-side error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BSAuthError(BSAPIError):
    """Raised when BS returns 401 Unauthorized."""


class BSServerError(BSAPIError):
    """Raised when BS returns 5xx Server Error."""


class BSClient:
    """Async HTTP client for the Bangumi-syncer internal MCP API.

    - base_url: configurable (env BS_BASE_URL or constructor arg)
    - Bearer token: per-request via token parameter, or fallback to constructor token / env BS_API_TOKEN
    - Parses the ``{"status":"success","data":{...}}`` envelope
    - Raises typed exceptions on error responses
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        trust_env: bool = True,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("BS_BASE_URL", "http://bs:8000")
        ).rstrip("/")
        self._token = token or os.environ.get("BS_API_TOKEN", "")
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": timeout,
            "headers": self._auth_headers(),
            "trust_env": trust_env,
        }
        if _transport is not None:
            kwargs["transport"] = _transport
            kwargs["trust_env"] = False
        self._client = httpx.AsyncClient(**kwargs)

    def _auth_headers(self) -> dict[str, str]:
        """Return Authorization header dict (empty if no token configured)."""
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _get_auth_headers(self, token: str | None = None) -> dict[str, str]:
        """Return Authorization header dict for a given token (or default)."""
        effective = token if token is not None else self._token
        if effective:
            return {"Authorization": f"Bearer {effective}"}
        return {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> Any:
        """Send HTTP request, parse envelope, raise on error.

        If token is provided, it overrides the default Authorization header for this request.
        """
        auth_headers = self._get_auth_headers(token)
        response = await self._client.request(
            method, path, params=params, json=json, headers=auth_headers
        )

        # Handle HTTP-level errors before envelope parsing
        if response.status_code == 401:
            raise BSAuthError(
                f"BS returned 401 Unauthorized: {response.text}",
                status_code=401,
            )
        if response.status_code >= 500:
            raise BSServerError(
                f"BS returned {response.status_code}: {response.text}",
                status_code=response.status_code,
            )

        # Parse envelope
        payload = response.json()
        status = payload.get("status")
        if status != "success":
            raise BSAPIError(
                f"BS returned non-success status '{status}': {payload.get('message', response.text)}",
                status_code=response.status_code,
            )
        return payload.get("data")

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def get_logs(
        self,
        *,
        level: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        since: str | None = None,
        until: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Fetch logs from BS. GET /api/mcp/logs"""
        params: dict[str, Any] = {}
        if level is not None:
            params["level"] = level
        if search is not None:
            params["search"] = search
        if limit is not None:
            params["limit"] = limit
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        return await self._request("GET", "/api/mcp/logs", params=params, token=token)

    async def get_current_config(self, token: str | None = None) -> dict[str, Any]:
        """Fetch current BS config. GET /api/mcp/config"""
        return await self._request("GET", "/api/mcp/config", token=token)

    async def update_config(
        self,
        *,
        section: str,
        key: str,
        value: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Update a BS config value. POST /api/mcp/config/update.

        BS contract: body is ``{section: {key, value}}`` — section name
        may use underscore (server auto-normalises to hyphen).
        """
        return await self._request(
            "POST",
            "/api/mcp/config/update",
            json={section: {key: value}},
            token=token,
        )

    async def close(self) -> None:
        """Close underlying HTTP client."""
        await self._client.aclose()
