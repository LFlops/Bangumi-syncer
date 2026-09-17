"""Bangumi-syncer 内置 MCP 服务（FastMCP 同仓嵌入实现）。

本包将 MCP 服务直接嵌入 BS 进程，不经 HTTP 调用业务层。模块构成：

- ``provider``：FastMCP OAuthProvider（RSA/RS256 密钥管理、authorize/token/register、
  consent 授权页与 CSRF 保护）。
- ``tools``：MCP 工具实现（get_logs / get_current_config / update_config），
  直接调用 BS 业务层。
- ``server``：FastMCP 实例装配，其中 ``create_mcp_server`` 是全仓唯一装配入口——
  ``/consent`` 路由必须在 ``http_app()`` 之前注册，故所有调用方（含测试）都应经由它装配。

公共入口通过子模块直接导入（如 ``from app.mcp.server import mcp_app``），本文件不做符号再导出。
"""
