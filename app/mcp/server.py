"""
FastMCP server 工厂

创建 FastMCP 实例并注册工具，生成 http_app，供 app/main.py 嵌入。
"""

import os
import tempfile

from fastmcp import FastMCP

from .provider import BangumiOAuthProvider, RSAKeyManager
from .tools import get_current_config, get_logs, update_config

# base_url 占位：生产环境应从配置读取公共 URL
_DEFAULT_BASE_URL = "http://localhost:8000"

# RSA key paths (configurable via environment)
_PRIVATE_KEY_PATH = os.environ.get(
    "MCP_RSA_PRIVATE_KEY", os.path.join(tempfile.gettempdir(), "mcp_private.pem")
)
_PUBLIC_KEY_PATH = os.environ.get(
    "MCP_RSA_PUBLIC_KEY", os.path.join(tempfile.gettempdir(), "mcp_public.pem")
)


def _create_provider(base_url: str = _DEFAULT_BASE_URL) -> BangumiOAuthProvider:
    """Create a BangumiOAuthProvider with RSA keys."""
    rsa_manager = RSAKeyManager(
        private_key_path=_PRIVATE_KEY_PATH,
        public_key_path=_PUBLIC_KEY_PATH,
    )
    rsa_manager.load_or_generate()

    return BangumiOAuthProvider(
        base_url=base_url,
        rsa_manager=rsa_manager,
        issuer=base_url,
        audience="bangumi-syncer",
        token_expiry_seconds=3600,
        auth_enabled=False,  # Will be overridden by config in production
        auth_username="admin",
    )


def _register_tools(mcp: FastMCP) -> None:
    """Register MCP tools on the FastMCP instance.

    Uses mcp.add_tool() to register async tool functions from app.mcp.tools.
    Each tool returns a {"status": "success", "data": ...} envelope and raises
    ToolError on failure.
    """
    mcp.add_tool(get_logs)
    mcp.add_tool(get_current_config)
    mcp.add_tool(update_config)


def create_mcp_server(base_url: str = _DEFAULT_BASE_URL) -> FastMCP:
    """创建 FastMCP 实例，使用 BangumiOAuthProvider 并注册工具。

    Args:
        base_url: 服务公共 URL，用于 OAuth metadata 中的 issuer / endpoint。

    Returns:
        FastMCP 实例（已配置 auth 并注册 3 个工具）。
    """
    provider = _create_provider(base_url=base_url)
    mcp = FastMCP(name="bangumi-syncer", auth=provider)
    _register_tools(mcp)
    return mcp


def create_mcp_app(base_url: str = _DEFAULT_BASE_URL):
    """创建 FastMCP http_app（Starlette app），用于路由提取与 lifespan 合并。

    Args:
        base_url: 服务公共 URL。

    Returns:
        StarletteWithLifespan 实例，包含：
        - /.well-known/oauth-authorization-server
        - /.well-known/oauth-protected-resource/mcp
        - /authorize
        - /token
        - /mcp（工具端点，含 3 个注册工具）
    """
    mcp = create_mcp_server(base_url=base_url)
    return mcp.http_app(path="/mcp")


# 模块级单例：供 app/main.py 直接导入
mcp_app = create_mcp_app()
