"""
FastMCP server 工厂

创建 FastMCP 实例并生成 http_app，供 app/main.py 嵌入。
"""

from fastmcp import FastMCP

from .provider import PlaceholderOAuthProvider

# base_url 占位：生产环境应从配置读取公共 URL
_DEFAULT_BASE_URL = "http://localhost:8000"


def create_mcp_server(base_url: str = _DEFAULT_BASE_URL) -> FastMCP:
    """创建 FastMCP 实例，使用 PlaceholderOAuthProvider 占位。

    Args:
        base_url: 服务公共 URL，用于 OAuth metadata 中的 issuer / endpoint。

    Returns:
        FastMCP 实例（已配置 auth）。
    """
    provider = PlaceholderOAuthProvider(base_url=base_url)
    return FastMCP(name="bangumi-syncer", auth=provider)


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
        - /mcp（工具端点）
    """
    mcp = create_mcp_server(base_url=base_url)
    return mcp.http_app(path="/mcp")


# 模块级单例：供 app/main.py 直接导入
mcp_app = create_mcp_app()
