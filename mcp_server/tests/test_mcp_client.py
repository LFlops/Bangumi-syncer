"""
Tests for BS internal API client (mcp_server.client).

Uses httpx.MockTransport to avoid real HTTP calls.
"""

import httpx
import pytest
import respx

from mcp_server.client import (
    BSAPIError,
    BSAuthError,
    BSClient,
    BSServerError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_client(handler: httpx.MockTransport) -> BSClient:
    """Create a BSClient with a mocked transport injected via DI."""
    transport = httpx.MockTransport(handler)
    return BSClient(base_url="http://bs:8000", token="test-jwt", _transport=transport)


def _success_envelope(data: dict | list) -> dict:
    """Wrap data in the BS success envelope."""
    return {"status": "success", "data": data}


# ---------------------------------------------------------------------------
# get_logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_logs_constructs_correct_url_and_params():
    """get_logs should hit GET /api/mcp/logs with query params."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url.path) == "/api/mcp/logs"
        assert "level" in request.url.params
        assert request.url.params["level"] == "error"
        assert request.url.params["limit"] == "50"
        return httpx.Response(200, json=_success_envelope({"logs": []}))

    client = _mock_client(handler)
    result = await client.get_logs(level="error", limit=50)
    assert result == {"logs": []}


@pytest.mark.asyncio
async def test_get_logs_injects_bearer_token():
    """Every request must carry Authorization: Bearer <jwt>."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer test-jwt"
        return httpx.Response(200, json=_success_envelope({"logs": []}))

    client = _mock_client(handler)
    await client.get_logs()


@pytest.mark.asyncio
async def test_get_logs_passes_since_until_search():
    """since/until/search params should be forwarded."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["since"] == "2026-01-01"
        assert request.url.params["until"] == "2026-09-03"
        assert request.url.params["search"] == "sync"
        return httpx.Response(200, json=_success_envelope({"logs": []}))

    client = _mock_client(handler)
    await client.get_logs(since="2026-01-01", until="2026-09-03", search="sync")


# ---------------------------------------------------------------------------
# get_current_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_config_no_params():
    """get_current_config should hit GET /api/mcp/config with no query params."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url.path) == "/api/mcp/config"
        assert len(request.url.params) == 0
        return httpx.Response(
            200, json=_success_envelope({"bangumi": {"token": "xxx"}})
        )

    client = _mock_client(handler)
    result = await client.get_current_config()
    assert result == {"bangumi": {"token": "xxx"}}


# ---------------------------------------------------------------------------
# update_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_config_sends_section_key_value():
    """update_config should POST /api/mcp/config/update with {section: {key: value}} body."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url.path) == "/api/mcp/config/update"
        import json

        body = json.loads(request.content)
        # BS contract: {section: {key, value}} — section name may use underscore (auto-normalized)
        assert body == {"dev": {"sync_interval": "60"}}
        return httpx.Response(
            200,
            json=_success_envelope(
                {"changed": [{"section": "dev", "key": "sync_interval"}]}
            ),
        )

    client = _mock_client(handler)
    result = await client.update_config(section="dev", key="sync_interval", value="60")
    assert result == {"changed": [{"section": "dev", "key": "sync_interval"}]}


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_raises_bs_auth_error():
    """BS returning 401 should raise BSAuthError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Unauthorized"})

    client = _mock_client(handler)
    with pytest.raises(BSAuthError):
        await client.get_current_config()


@pytest.mark.asyncio
async def test_5xx_raises_bs_server_error():
    """BS returning 5xx should raise BSServerError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "Service Unavailable"})

    client = _mock_client(handler)
    with pytest.raises(BSServerError):
        await client.get_logs()


@pytest.mark.asyncio
async def test_non_success_status_raises_bs_api_error():
    """BS returning {"status":"error",...} should raise BSAPIError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "message": "bad input"})

    client = _mock_client(handler)
    with pytest.raises(BSAPIError):
        await client.update_config(section="x", key="y", value="z")


@pytest.mark.asyncio
async def test_network_error_propagates():
    """Network-level errors should propagate as httpx.TransportError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection refused")

    client = _mock_client(handler)
    with pytest.raises(httpx.ConnectTimeout):
        await client.get_logs()


# ---------------------------------------------------------------------------
# respx-based alternative test (demonstrates respx usage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_logs_with_respx():
    """Same get_logs test using respx for mock routing."""
    async with respx.mock(base_url="http://bs:8000") as mock:
        route = mock.get("/api/mcp/logs").mock(
            return_value=httpx.Response(
                200, json=_success_envelope({"logs": [{"line": "ok"}]})
            )
        )
        # trust_env=False avoids picking up SOCKS proxy from environment
        client = BSClient(base_url="http://bs:8000", token="abc", trust_env=False)
        result = await client.get_logs()
        assert route.called
        assert result == {"logs": [{"line": "ok"}]}
        assert route.calls.last.request.headers["authorization"] == "Bearer abc"
