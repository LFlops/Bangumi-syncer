"""
main.py MCP 集成测试

覆盖 BDD 场景：
1. 路由注册：app 包含 /api/mcp/* 路由
2. 公钥加载：lifespan 启动时公钥文件存在 → 加载成功
3. 公钥缺失：lifespan 启动时公钥文件不存在 → 记录 warning 日志，不崩溃
4. 集成：GET /api/mcp/config 无凭证 → 401
5. 公钥路径可配：MCP_PUBLIC_KEY_PATH 环境变量覆盖默认路径
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from app.core import mcp_auth

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_rsa_pem_keypair() -> tuple[bytes, bytes]:
    """生成 RSA 密钥对，返回 (private_pem, public_pem)。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def tmp_public_key(tmp_path: Path) -> Path:
    """生成临时 RSA 公钥文件，返回其路径。"""
    _, public_pem = _generate_rsa_pem_keypair()
    key_file = tmp_path / "mcp_public.pem"
    key_file.write_bytes(public_pem)
    return key_file


# ---------------------------------------------------------------------------
# 1. 路由注册
# ---------------------------------------------------------------------------


class TestMcpRouteRegistration:
    """验证 app.main.app 注册了 /api/mcp 路由。"""

    def test_main_app_contains_api_mcp_routes(self):
        """main app 应包含前缀为 /api/mcp 的路由。"""
        from app.main import app

        mcp_routes = [
            route
            for route in app.routes
            if hasattr(route, "path") and route.path.startswith("/api/mcp")
        ]
        assert len(mcp_routes) > 0, "app 应注册 /api/mcp/* 路由"

    def test_main_app_contains_config_route(self):
        """main app 应注册 /api/mcp/config 路由。"""
        from app.main import app

        config_routes = [
            route
            for route in app.routes
            if hasattr(route, "path") and route.path == "/api/mcp/config"
        ]
        assert len(config_routes) > 0, "app 应注册 /api/mcp/config 路由"


# ---------------------------------------------------------------------------
# 2. 公钥加载
# ---------------------------------------------------------------------------


class TestLifespanPublicKeyLoading:
    """验证 lifespan 启动时公钥初始化行为。"""

    @pytest.mark.asyncio
    async def test_lifespan_loads_public_key_when_file_exists(
        self, tmp_public_key: Path, monkeypatch
    ):
        """公钥文件存在时，lifespan 启动应成功加载公钥。"""
        # 清除缓存，确保从文件加载
        mcp_auth._public_key_cache.clear()
        monkeypatch.setenv("MCP_PUBLIC_KEY_PATH", str(tmp_public_key))

        from fastapi import FastAPI

        from app.main import lifespan

        app = FastAPI()

        async with lifespan(app):
            # lifespan 内公钥应已加载并缓存
            assert str(tmp_public_key) in mcp_auth._public_key_cache

    @pytest.mark.asyncio
    async def test_lifespan_logs_warning_when_public_key_missing(self, monkeypatch):
        """公钥文件不存在时，lifespan 应记录 warning 日志且不崩溃。"""
        mcp_auth._public_key_cache.clear()
        monkeypatch.setenv("MCP_PUBLIC_KEY_PATH", "/nonexistent/path/mcp_public.pem")

        from fastapi import FastAPI

        from app.main import lifespan

        app = FastAPI()

        # 不应抛出异常，且应记录包含 "MCP 公钥" 的 warning 日志
        with patch("app.main.logger") as mock_logger:
            async with lifespan(app):
                pass

            assert mock_logger.warning.called, "公钥缺失时应调用 logger.warning"
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "MCP 公钥" in warning_msg, (
                f"warning 消息应包含 'MCP 公钥'，实际为: {warning_msg}"
            )


# ---------------------------------------------------------------------------
# 4. 集成测试
# ---------------------------------------------------------------------------


class TestMcpAuthIntegration:
    """验证路由 + 鉴权链路连通。"""

    @pytest.mark.asyncio
    async def test_get_config_without_credentials_returns_401(self):
        """GET /api/mcp/config 无凭证 → 401。"""
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/mcp/config")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_config_with_missing_public_key_returns_503(self, monkeypatch):
        """公钥缺失时请求 /api/mcp/config → 503。"""
        # 确保公钥不在缓存中且路径指向不存在文件
        mcp_auth._public_key_cache.clear()
        monkeypatch.setattr(
            "app.api.deps._MCP_PUBLIC_KEY_PATH",
            "/nonexistent/mcp_public.pem",
        )

        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/mcp/config", headers={"Authorization": "Bearer fake.jwt.token"}
            )

        assert response.status_code == 503


# ---------------------------------------------------------------------------
# 5. 公钥路径可配
# ---------------------------------------------------------------------------


class TestPublicKeyPathConfigurable:
    """验证 MCP_PUBLIC_KEY_PATH 环境变量可覆盖默认路径。"""

    def test_env_var_overrides_default_path(self, monkeypatch):
        """设置 MCP_PUBLIC_KEY_PATH 后，_get_mcp_public_key_path 应返回该路径。"""
        custom_path = "/custom/path/mcp_public.pem"
        monkeypatch.setenv("MCP_PUBLIC_KEY_PATH", custom_path)

        from app.main import _get_mcp_public_key_path

        assert _get_mcp_public_key_path() == custom_path

    def test_default_path_when_env_not_set(self, monkeypatch):
        """未设置 MCP_PUBLIC_KEY_PATH 时使用默认路径。"""
        monkeypatch.delenv("MCP_PUBLIC_KEY_PATH", raising=False)

        from app.main import _get_mcp_public_key_path

        assert _get_mcp_public_key_path() == "/mcp_auth/mcp_public.pem"

    @pytest.mark.asyncio
    async def test_lifespan_syncs_custom_path_to_deps(self, monkeypatch):
        """lifespan 启动时应将自定义公钥路径同步到 deps 模块。"""
        custom_path = "/custom/sync/path/mcp_public.pem"
        monkeypatch.setenv("MCP_PUBLIC_KEY_PATH", custom_path)

        from fastapi import FastAPI

        from app.main import lifespan

        app = FastAPI()

        from app.api import deps

        original_path = deps._MCP_PUBLIC_KEY_PATH
        try:
            async with lifespan(app):
                assert deps._MCP_PUBLIC_KEY_PATH == custom_path
        finally:
            deps._MCP_PUBLIC_KEY_PATH = original_path
