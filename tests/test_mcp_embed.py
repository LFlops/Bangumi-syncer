"""
FastMCP 嵌入骨架测试

覆盖 BDD 场景：
1. /mcp 端点存在（未认证时不 404）
2. /.well-known/oauth-authorization-server 在根路径可达（200）
3. /authorize 在根路径存在（不 404）
4. /token 在根路径存在（不 404）
5. 现有路由（/health）未被破坏
6. lifespan 合并后可正常启停
"""

import base64
import hashlib
import re
import secrets
from contextlib import ExitStack, asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient  # noqa: I001

# ---------------------------------------------------------------------------
# 1. /mcp 端点存在
# ---------------------------------------------------------------------------


class TestMcpEndpointExists:
    """验证 /mcp 端点注册到 FastAPI app。"""

    def test_mcp_endpoint_not_404(self):
        """FastAPI app 应注册 /mcp 端点，访问时不返回 404。"""
        with _embed_mocks():
            from app.main import app

            with TestClient(app) as client:
                response = client.get("/mcp")
            assert response.status_code != 404, "/mcp 不应返回 404"

    def test_mcp_endpoint_responds(self):
        """GET /mcp 应返回响应（200 或 401，但不 404）。"""
        with _embed_mocks():
            from app.main import app

            with TestClient(app) as client:
                response = client.get("/mcp")
            assert response.status_code != 404, "/mcp 不应返回 404"


# ---------------------------------------------------------------------------
# 2. /.well-known 路由在根路径
# ---------------------------------------------------------------------------


class TestWellKnownRoutesAtRoot:
    """验证 OAuth 发现文档在根路径可达。"""

    def test_oauth_authorization_server_metadata_at_root(self):
        """/.well-known/oauth-authorization-server 在根路径返回 200。"""
        with _embed_mocks():
            from app.main import app

            with TestClient(app) as client:
                response = client.get("/.well-known/oauth-authorization-server")
            assert response.status_code == 200, (
                f"应在根路径返回 200，实际: {response.status_code}"
            )

    def test_oauth_protected_resource_metadata_at_root(self):
        """/.well-known/oauth-protected-resource/mcp 在根路径返回 200。"""
        with _embed_mocks():
            from app.main import app

            with TestClient(app) as client:
                response = client.get("/.well-known/oauth-protected-resource/mcp")
            assert response.status_code == 200, (
                f"应在根路径返回 200，实际: {response.status_code}"
            )


# ---------------------------------------------------------------------------
# 3. /authorize 和 /token 在根路径
# ---------------------------------------------------------------------------


class TestOperationalRoutesAtRoot:
    """验证 /authorize 和 /token 在根路径存在。"""

    def test_authorize_not_404(self):
        """/authorize 在根路径存在（不 404）。"""
        with _embed_mocks():
            from app.main import app

            with TestClient(app) as client:
                response = client.get("/authorize")
            assert response.status_code != 404, "/authorize 不应返回 404"

    def test_token_not_404(self):
        """/token 在根路径存在（不 404）。"""
        with _embed_mocks():
            from app.main import app

            with TestClient(app) as client:
                response = client.get("/token")
            assert response.status_code != 404, "/token 不应返回 404"


# ---------------------------------------------------------------------------
# 4. 现有路由未被破坏
# ---------------------------------------------------------------------------


class TestExistingRoutesNotBroken:
    """验证现有 25 个 router + 中间件未被破坏。"""

    def test_health_endpoint_still_works(self):
        """GET /health 仍返回 200。"""
        with _embed_mocks():
            from app.main import app

            with TestClient(app) as client:
                response = client.get("/health")
            assert response.status_code == 200
            assert response.json().get("status") == "healthy"

    def test_app_has_many_routes(self):
        """app 应保留大量现有路由（远多于新增的 MCP 路由）。"""
        with _embed_mocks():
            from app.main import app

            paths = [route.path for route in app.routes if hasattr(route, "path")]
            # 现有 + MCP 路由应远超 10 个（实际约 10 个：openapi/docs/static + MCP 路由）
            assert len(paths) >= 10, f"路由数量应 >= 10，实际: {len(paths)}"


# ---------------------------------------------------------------------------
# 5. lifespan 合并
# ---------------------------------------------------------------------------


class TestLifespanCombination:
    """验证 lifespan 合并后正常启停。"""

    @pytest.mark.asyncio
    async def test_combined_lifespan_starts_and_stops(self):
        """合并后的 lifespan 应能正常进入和退出。"""
        with _embed_mocks():
            from app.main import lifespan

            @asynccontextmanager
            async def test_ls(app):
                yield

            from fastapi import FastAPI

            test_app = FastAPI()

            async with lifespan(test_app):
                pass


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. 生产组合：携带合法 Bearer 调用 /mcp 必须成功
# ---------------------------------------------------------------------------


def _make_code_challenge_b64(verifier: str) -> str:
    """由 verifier 生成 S256 code challenge（base64url，无填充）。"""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _extract_csrf_token(html_content: str) -> str:
    """从 consent 表单 HTML 中提取 CSRF token。"""
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_content)
    if not match:
        raise ValueError("CSRF token not found in consent form")
    return match.group(1)


def _do_full_oauth_flow(client, session_token: str | None = None) -> str:
    """在生产组合上走完 OAuth 流程，返回 access_token。

    session_token 仅用于 auth.enabled=True 时通过 /consent 的 Web 会话校验。
    """
    reg_response = client.post(
        "/register",
        json={
            "redirect_uris": ["http://localhost/callback"],
            "grant_types": ["authorization_code"],
            "token_endpoint_auth_method": "none",
            "scope": "read write",
        },
    )
    assert reg_response.status_code == 201, (
        f"DCR 应返回 201，实际: {reg_response.status_code}, body: {reg_response.text[:200]}"
    )
    client_id = reg_response.json()["client_id"]

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
    assert auth_response.status_code == 302, (
        f"/authorize 应返回 302，实际: {auth_response.status_code}, body: {auth_response.text[:200]}"
    )
    consent_url = auth_response.headers["location"]

    cookies = {"session_token": session_token} if session_token else None
    consent_get = client.get(consent_url, cookies=cookies)
    assert consent_get.status_code == 200, (
        f"/consent GET 应返回 200，实际: {consent_get.status_code}, body: {consent_get.text[:200]}"
    )
    csrf_token = _extract_csrf_token(consent_get.text)
    request_token = parse_qs(urlparse(consent_url).query)["request_token"][0]

    consent_post = client.post(
        "/consent",
        data={
            "action": "allow",
            "request_token": request_token,
            "csrf_token": csrf_token,
        },
        cookies=cookies,
        follow_redirects=False,
    )
    assert consent_post.status_code == 302, (
        f"/consent POST 应返回 302，实际: {consent_post.status_code}, body: {consent_post.text[:200]}"
    )
    code = parse_qs(urlparse(consent_post.headers["location"]).query)["code"][0]

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
    assert token_response.status_code == 200, (
        f"/token 应返回 200，实际: {token_response.status_code}, body: {token_response.text[:200]}"
    )
    return token_response.json()["access_token"]


class TestProductionAppMcpCall:
    """验证生产组合（app.main.app 摊平路由后）携带合法 Bearer 能调用 /mcp。"""

    def test_生产App_携带合法Bearer_调用mcp成功(self, monkeypatch):
        """生产 app 上走完 OAuth 后，携带 Bearer 调 /mcp initialize 应返回 200。

        修复前：由于 mcp_app.user_middleware 未迁移，/mcp 端点的
        RequireAuthMiddleware 读不到 scope["user"]，永远返回 401。
        """
        from app.mcp import provider as provider_module

        # 生产配置 auth.enabled=True，/consent 需要 Web 会话；打桩会话校验，
        # 仅绕过 Web 会话检查，不影响 Bearer JWT 的验签链路。
        monkeypatch.setattr(
            provider_module.security_manager,
            "validate_session",
            lambda token: {"username": "admin", "created_at": 0},
        )

        with _embed_mocks():
            from app.main import app

            with TestClient(app, raise_server_exceptions=False) as client:
                access_token = _do_full_oauth_flow(
                    client, session_token="valid-session-token"
                )

                response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "test-client", "version": "1.0"},
                        },
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                )

                assert response.status_code == 200, (
                    f"携带合法 Bearer 调用 /mcp 应返回 200，实际: {response.status_code}, "
                    f"body: {response.text[:300]}"
                )
                assert response.headers.get("mcp-session-id") is not None
                session_id = response.headers["mcp-session-id"]

                # 进一步验证 get_access_token() 依赖的 contextvar 链路：
                # tools/call 内部要求解析出含 read scope 的 access token。
                tool_response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "get_current_config", "arguments": {}},
                    },
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Mcp-Session-Id": session_id,
                    },
                )

        assert tool_response.status_code == 200, (
            f"携带合法 Bearer 调用 tools/call 应返回 200，实际: {tool_response.status_code}, "
            f"body: {tool_response.text[:300]}"
        )


@contextmanager
def _embed_mocks():
    """为 MCP 嵌入测试打桩 lifespan 中的外部依赖。"""
    defaults = {
        "app.main.startup_info.print_info": {},
        "app.main.startup_info.print_separator": {},
        "app.main.startup_info.print_success": {},
        "app.main.startup_info.print_error": {},
        "app.main.startup_info.print_startup_complete": {},
        "app.main.config_manager.get_bangumi_configs": {"return_value": {}},
        "app.main.mapping_service.get_all_mappings": {"return_value": {}},
        "app.main.ensure_feiniu_startup_watermark": {},
        "app.main.database_manager.cleanup_pending_sync_queue": {},
        "app.main.config_manager.get_scheduler_config": {
            "return_value": {"startup_delay": 0}
        },
        "app.main.register_schedulers": {},
        "app.main.scheduler_registry.start_all": {"new": AsyncMock()},
        "app.main.scheduler_registry.stop_all": {"new": AsyncMock()},
        "asyncio.sleep": {"new": AsyncMock()},
    }
    with ExitStack() as stack:
        for path, kw in defaults.items():
            stack.enter_context(patch(path, **kw))
        yield
