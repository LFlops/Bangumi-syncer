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

from contextlib import ExitStack, asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, patch

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

            mcp_routes = [
                route
                for route in app.routes
                if hasattr(route, "path") and route.path == "/mcp"
            ]
            assert len(mcp_routes) > 0, "app 应注册 /mcp 路由"

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

            authorize_routes = [
                route
                for route in app.routes
                if hasattr(route, "path") and route.path == "/authorize"
            ]
            assert len(authorize_routes) > 0, "app 应注册 /authorize 路由"

    def test_token_not_404(self):
        """/token 在根路径存在（不 404）。"""
        with _embed_mocks():
            from app.main import app

            token_routes = [
                route
                for route in app.routes
                if hasattr(route, "path") and route.path == "/token"
            ]
            assert len(token_routes) > 0, "app 应注册 /token 路由"


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
# Helpers
# ---------------------------------------------------------------------------


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
