"""
FastMCP server 工厂

创建 FastMCP 实例并注册工具，生成 http_app，供 app/main.py 嵌入。
"""

import os
import tempfile

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import config_manager
from app.core.security import security_manager

from .provider import (
    REFRESH_TOKEN_TTL,
    BangumiOAuthProvider,
    RSAKeyManager,
    handle_consent,
)
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


def _resolve_base_url(base_url: str | None = None) -> str:
    """解析 base_url：优先参数 > 环境变量 MCP_BASE_URL > 配置 > 默认值。"""
    if base_url:
        return base_url
    env_url = os.environ.get("MCP_BASE_URL")
    if env_url:
        return env_url
    # 从 dev 配置读取公共 URL（如有）
    configured = config_manager.get("dev", "mcp_base_url", fallback="")
    if configured:
        return configured
    return _DEFAULT_BASE_URL


def _create_provider(base_url: str | None = None) -> BangumiOAuthProvider:
    """Create a BangumiOAuthProvider with RSA keys.

    auth_enabled / auth_username 从 BS 安全配置读取；
    base_url / 优先参数 > MCP_BASE_URL 环境变量 > 配置 > 默认值。
    """
    resolved_base_url = _resolve_base_url(base_url)
    auth_config = security_manager.get_auth_config()

    rsa_manager = RSAKeyManager(
        private_key_path=_PRIVATE_KEY_PATH,
        public_key_path=_PUBLIC_KEY_PATH,
    )
    rsa_manager.load_or_generate()

    refresh_ttl = int(os.environ.get("MCP_REFRESH_TOKEN_TTL", str(REFRESH_TOKEN_TTL)))

    return BangumiOAuthProvider(
        base_url=resolved_base_url,
        rsa_manager=rsa_manager,
        issuer=resolved_base_url,
        audience="bangumi-syncer",
        token_expiry_seconds=3600,
        refresh_token_ttl=refresh_ttl,
        auth_enabled=bool(auth_config["enabled"]),
        auth_username=str(auth_config["username"]),
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


def create_mcp_server(base_url: str | None = None) -> FastMCP:
    """创建 FastMCP 实例，使用 BangumiOAuthProvider 并注册工具。

    Args:
        base_url: 服务公共 URL，用于 OAuth metadata 中的 issuer / endpoint。
                  None 时从配置/环境变量自动解析。

    Returns:
        FastMCP 实例（已配置 auth、注册 /consent 路由、注册 3 个工具）。
    """
    provider = _create_provider(base_url=base_url)
    mcp = FastMCP(name="bangumi-syncer", auth=provider)

    # 注册 /consent 路由（必须在 http_app() 调用前完成）
    @mcp.custom_route("/consent", methods=["GET", "POST"])
    async def consent_route(request: Request) -> Response:
        return await handle_consent(request, provider)

    _register_tools(mcp)
    return mcp


def create_mcp_app(base_url: str | None = None):
    """创建 FastMCP http_app（Starlette app），用于路由提取与 lifespan 合并。

    Args:
        base_url: 服务公共 URL。None 时从配置/环境变量自动解析。

    Returns:
        StarletteWithLifespan 实例，包含：
        - /.well-known/oauth-authorization-server
        - /.well-known/oauth-protected-resource/mcp
        - /authorize
        - /token
        - /consent
        - /mcp（工具端点，含 3 个注册工具）
    """
    mcp = create_mcp_server(base_url=base_url)
    return mcp.http_app(path="/mcp")


# 模块级单例：供 app/main.py 直接导入
mcp_app = create_mcp_app()
