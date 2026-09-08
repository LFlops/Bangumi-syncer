"""
main.py MCP 集成测试

覆盖 BDD 场景：
1. 路由注册：app 不包含 /api/mcp/* 路由（已删除死路由）
2. MCP OAuth 路由存在：app 包含 /.well-known、/authorize、/token、/mcp 路由
"""

# ---------------------------------------------------------------------------
# 1. 路由注册：/api/mcp 已删除
# ---------------------------------------------------------------------------


class TestApiMcpRoutesRemoved:
    """验证 app.main.app 不再注册 /api/mcp 路由（死路由已删除）。"""

    def test_main_app_does_not_contain_api_mcp_routes(self):
        """main app 不应包含前缀为 /api/mcp 的路由。"""
        from app.main import app

        mcp_routes = [
            route
            for route in app.routes
            if hasattr(route, "path") and route.path.startswith("/api/mcp")
        ]
        assert len(mcp_routes) == 0, (
            f"app 不应注册 /api/mcp/* 路由（死路由已删除），实际: {[r.path for r in mcp_routes]}"
        )


# ---------------------------------------------------------------------------
# 2. MCP OAuth 路由存在
# ---------------------------------------------------------------------------


class TestMcpOAuthRoutesExist:
    """验证 app 注册了 MCP OAuth 相关路由（/.well-known、/authorize、/token、/mcp）。"""

    def test_main_app_contains_mcp_endpoint(self):
        """main app 应注册 /mcp 路由。"""
        from app.main import app

        mcp_routes = [
            route
            for route in app.routes
            if hasattr(route, "path") and route.path == "/mcp"
        ]
        assert len(mcp_routes) > 0, "app 应注册 /mcp 路由"

    def test_main_app_contains_well_known_routes(self):
        """main app 应注册 /.well-known/oauth-authorization-server 路由。"""
        from app.main import app

        well_known_routes = [
            route
            for route in app.routes
            if hasattr(route, "path") and ".well-known" in route.path
        ]
        assert len(well_known_routes) > 0, "app 应注册 /.well-known/* 路由"

    def test_main_app_contains_authorize_route(self):
        """main app 应注册 /authorize 路由。"""
        from app.main import app

        authorize_routes = [
            route
            for route in app.routes
            if hasattr(route, "path") and route.path == "/authorize"
        ]
        assert len(authorize_routes) > 0, "app 应注册 /authorize 路由"

    def test_main_app_contains_token_route(self):
        """main app 应注册 /token 路由。"""
        from app.main import app

        token_routes = [
            route
            for route in app.routes
            if hasattr(route, "path") and route.path == "/token"
        ]
        assert len(token_routes) > 0, "app 应注册 /token 路由"
