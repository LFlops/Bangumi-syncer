"""
MCP 端到端集成测试

覆盖 BDD 场景：
1. list_tools → 3 工具且 schema 正确
2. 未认证调工具 → 401
3. 走完授权流程 → token → 调工具成功
4. /.well-known/oauth-authorization-server 含 client_id_metadata_document_supported: true
"""

from __future__ import annotations

import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from fastmcp import Client
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_code_challenge_b64(verifier: str) -> str:
    """Create S256 code challenge from verifier (base64url)."""
    import base64

    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _extract_csrf_token(html_content: str) -> str:
    """Extract CSRF token from consent form HTML."""
    import re

    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_content)
    if not match:
        raise ValueError("CSRF token not found in consent form")
    return match.group(1)


def _create_test_server_with_tools(
    private_key_path: str,
    public_key_path: str,
    issuer: str,
    audience: str,
    auth_enabled: bool = False,
    auth_username: str = "admin",
):
    """Create a test server with both OAuth auth and MCP tools registered.

    This combines create_auth_server (consent flow) with create_mcp_server (tools).
    """
    from fastmcp import FastMCP
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

    from app.mcp.provider import (
        BangumiOAuthProvider,
        RSAKeyManager,
        handle_consent,
    )
    from app.mcp.server import _register_tools

    # Initialize RSA keys
    rsa_manager = RSAKeyManager(
        private_key_path=private_key_path,
        public_key_path=public_key_path,
    )
    rsa_manager.load_or_generate()

    # Create provider
    provider = BangumiOAuthProvider(
        base_url=issuer,
        rsa_manager=rsa_manager,
        issuer=issuer,
        audience=audience,
        token_expiry_seconds=3600,
        auth_enabled=auth_enabled,
        auth_username=auth_username,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )

    # Create MCP server with auth
    mcp = FastMCP(name="bangumi-syncer-mcp", auth=provider)

    # Register tools
    _register_tools(mcp)

    # Register consent route BEFORE calling http_app
    @mcp.custom_route("/consent", methods=["GET", "POST"])
    async def consent_route(request):
        return await handle_consent(request, provider)

    # Get the Starlette app
    app = mcp.http_app(path="/mcp")

    # Store public key and provider on app state for testing
    app.state.public_key_pem = rsa_manager.get_public_key_pem()
    app.state.provider = provider

    return app


# ---------------------------------------------------------------------------
# 1. list_tools → 3 工具且 schema 正确
# ---------------------------------------------------------------------------


class TestListTools:
    """验证 MCP server 注册了 3 个工具且 schema 正确。"""

    @pytest.mark.asyncio
    async def test_list_tools_返回3个工具(self):
        """list_tools 应返回 3 个工具：get_logs / get_current_config / update_config。"""
        from app.mcp.server import create_mcp_server

        mcp = create_mcp_server()
        client = Client(mcp)
        async with client:
            tools = await client.list_tools()

        tool_names = [t.name for t in tools]
        assert len(tool_names) == 3, f"应注册 3 个工具，实际: {len(tool_names)}"
        assert "get_logs" in tool_names
        assert "get_current_config" in tool_names
        assert "update_config" in tool_names

    @pytest.mark.asyncio
    async def test_get_logs_schema_包含预期参数(self):
        """get_logs 工具的 schema 应包含 level/search/limit/since/until 参数。"""
        from app.mcp.server import create_mcp_server

        mcp = create_mcp_server()
        client = Client(mcp)
        async with client:
            tools = await client.list_tools()

        get_logs_tool = next(t for t in tools if t.name == "get_logs")
        params = get_logs_tool.input_schema.get("properties", {})
        assert "level" in params
        assert "search" in params
        assert "limit" in params
        assert "since" in params
        assert "until" in params

    @pytest.mark.asyncio
    async def test_get_current_config_schema_无参数(self):
        """get_current_config 工具应无参数。"""
        from app.mcp.server import create_mcp_server

        mcp = create_mcp_server()
        client = Client(mcp)
        async with client:
            tools = await client.list_tools()

        get_config_tool = next(t for t in tools if t.name == "get_current_config")
        params = get_config_tool.input_schema.get("properties", {})
        assert len(params) == 0, (
            f"get_current_config 应无参数，实际: {list(params.keys())}"
        )

    @pytest.mark.asyncio
    async def test_update_config_schema_包含必填参数(self):
        """update_config 工具的 schema 应包含 section/key/value 三个必填参数。"""
        from app.mcp.server import create_mcp_server

        mcp = create_mcp_server()
        client = Client(mcp)
        async with client:
            tools = await client.list_tools()

        update_tool = next(t for t in tools if t.name == "update_config")
        params = update_tool.input_schema.get("properties", {})
        required = update_tool.input_schema.get("required", [])
        assert "section" in params
        assert "key" in params
        assert "value" in params
        assert set(required) == {"section", "key", "value"}


# ---------------------------------------------------------------------------
# 2. 未认证调工具 → 401
# ---------------------------------------------------------------------------


class TestUnauthenticatedToolCall:
    """验证未认证时调用工具返回 401。"""

    @pytest.mark.asyncio
    async def test_未认证调工具_返回401(self):
        """未携带 Bearer Token 调用 /mcp 工具端点应返回 401。"""
        from app.mcp.server import create_mcp_app

        app = create_mcp_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_current_config", "arguments": {}},
                },
            )

        assert response.status_code == 401, (
            f"未认证调工具应返回 401，实际: {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_无效token_返回401(self):
        """携带无效 Bearer Token 调用工具应返回 401。"""
        from app.mcp.server import create_mcp_app

        app = create_mcp_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_current_config", "arguments": {}},
                },
                headers={"Authorization": "Bearer invalid.token.here"},
            )

        assert response.status_code == 401, (
            f"无效 token 应返回 401，实际: {response.status_code}"
        )


# ---------------------------------------------------------------------------
# 3. 走完授权流程 → token → 调工具成功
# ---------------------------------------------------------------------------


class TestFullOAuthFlowWithToolCall:
    """验证完整 OAuth 流程后能成功调用工具。"""

    @pytest.fixture
    def tmp_keys(self, tmp_path):
        """Create temporary key files."""
        return {
            "private": str(tmp_path / "private.pem"),
            "public": str(tmp_path / "public.pem"),
        }

    @pytest.fixture
    def server_app(self, tmp_keys):
        """Create a test server with auth.enabled=False and tools registered."""
        return _create_test_server_with_tools(
            private_key_path=tmp_keys["private"],
            public_key_path=tmp_keys["public"],
            issuer="http://localhost:8000",
            audience="bangumi-syncer",
            auth_enabled=False,
            auth_username="admin",
        )

    def _make_test_client(self, app):
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.asyncio
    async def test_授权后_token有效且能调用工具函数(self, server_app):
        """完整 OAuth 流程后，access_token 应有效且能调用工具函数。

        注意：MCP /mcp 端点需要 FastMCP 内部任务组（通过 FastAPI combine_lifespan 初始化），
        这里测试 OAuth 流程 + 工具函数直接调用，验证端到端集成。
        """
        # Steps 1-7: OAuth 流程（同步 TestClient 处理重定向/表单）
        client = self._make_test_client(server_app)

        # Step 1: DCR 注册客户端
        reg_response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert reg_response.status_code == 201
        client_id = reg_response.json()["client_id"]

        # Step 2: 发起授权
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = _make_code_challenge_b64(code_verifier)
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "scope": "read write",
                "state": "test-state",
            },
            follow_redirects=False,
        )
        assert auth_response.status_code == 302
        consent_url = auth_response.headers["location"]
        assert "/consent" in consent_url

        # Step 3: 获取 consent 页面并提取 CSRF token
        consent_get = client.get(consent_url)
        assert consent_get.status_code == 200
        csrf_token = _extract_csrf_token(consent_get.text)

        # Step 4: 解析 request_token
        parsed = urlparse(consent_url)
        request_token = parse_qs(parsed.query)["request_token"][0]

        # Step 5: 同意授权
        consent_post = client.post(
            "/consent",
            data={
                "action": "allow",
                "request_token": request_token,
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert consent_post.status_code == 302
        redirect_url = consent_post.headers["location"]
        assert "code=" in redirect_url

        # Step 6: 提取 authorization code
        parsed_redirect = urlparse(redirect_url)
        code = parse_qs(parsed_redirect.query)["code"][0]

        # Step 7: 换取 access token
        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://localhost/callback",
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
        assert token_response.status_code == 200
        token_data = token_response.json()
        assert token_data["token_type"] == "Bearer"
        assert token_data["access_token"]
        access_token = token_data["access_token"]

        # Step 8: 验证 access_token 有效（通过 provider 验签）
        provider = server_app.state.provider
        loaded_token = await provider.load_access_token(access_token)
        assert loaded_token is not None, "access_token 应能通过 provider 验签"
        assert "read" in loaded_token.scopes
        assert "write" in loaded_token.scopes

        # Step 9: 直接调用工具函数（验证工具在 server 注册后可正常执行）
        from app.mcp.tools import get_current_config

        result = await get_current_config()
        assert result["status"] == "success"
        assert "data" in result


# ---------------------------------------------------------------------------
# 4. /.well-known 端点含 client_id_metadata_document_supported: true
# ---------------------------------------------------------------------------


class TestWellKnownEndpoints:
    """验证 OAuth 发现文档端点。"""

    @pytest.mark.asyncio
    async def test_metadata_包含_cimd_support_flag(self):
        """/.well-known/oauth-authorization-server 应包含 client_id_metadata_document_supported: true。"""
        from app.mcp.server import create_mcp_app

        app = create_mcp_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/.well-known/oauth-authorization-server")

        assert response.status_code == 200
        data = response.json()
        assert data.get("client_id_metadata_document_supported") is True

    @pytest.mark.asyncio
    async def test_metadata_包含必要字段(self):
        """metadata 应包含 issuer / authorization_endpoint / token_endpoint / registration_endpoint。"""
        from app.mcp.server import create_mcp_app

        app = create_mcp_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/.well-known/oauth-authorization-server")

        assert response.status_code == 200
        data = response.json()
        assert "issuer" in data
        assert "authorization_endpoint" in data
        assert "token_endpoint" in data
        assert "registration_endpoint" in data

    @pytest.mark.asyncio
    async def test_protected_resource_metadata_可达(self):
        """/.well-known/oauth-protected-resource/mcp 应返回 200。"""
        from app.mcp.server import create_mcp_app

        app = create_mcp_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/.well-known/oauth-protected-resource/mcp")

        assert response.status_code == 200
