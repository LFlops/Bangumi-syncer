"""测试专用 MCP 装配 helper。

测试不再自行复制一份「FastMCP + /consent + http_app」的装配逻辑，而是
复用生产唯一装配入口 ``create_mcp_app``；这样 ``/consent`` 必须在
``http_app()`` 之前注册这类隐蔽约束只存在于生产代码一处，测试不会与
生产路径产生漂移。

本模块仅提供测试装配，不参与 pytest 收集（文件名不匹配 ``test_*.py``
/ ``*_test.py``）。
"""

from __future__ import annotations

from typing import Any

from app.mcp.provider import BangumiOAuthProvider, RSAKeyManager
from app.mcp.server import create_mcp_app


def build_test_mcp_app(
    private_key_path: str,
    public_key_path: str,
    issuer: str,
    audience: str,
    token_expiry_seconds: int = 3600,
    *,
    auth_enabled: bool = False,
    auth_username: str = "admin",
    base_url: str | None = None,
) -> Any:
    """构造一个启用 auth 的 Starlette app，供测试使用。

    复用生产唯一装配入口 ``create_mcp_app(provider=...)``，避免测试复制
    ``/consent`` 注册时机逻辑。provider 的 ``client_registration_options``
    与 ``revocation_options`` 不显式传入，走 ``BangumiOAuthProvider`` 默认值
    （``valid_scopes=["read", "write"]``、revocation 启用），与生产一致。

    Args:
        private_key_path: RSA 私钥路径。
        public_key_path: RSA 公钥路径。
        issuer: OAuth issuer（写入 metadata 与 JWT ``iss``）。
        audience: JWT ``aud``，测试用 ``jwt.decode(..., audience=...)`` 校验。
        token_expiry_seconds: access token 有效期。
        auth_enabled: 是否启用真人授权（consent）环节。
        auth_username: 授权时允许的用户名。
        base_url: 服务公共 URL，None 时回退到 ``issuer``。

    Returns:
        带 OAuth 端点与 consent 页面的 Starlette app；``app.state`` 上附带
        ``public_key_pem`` 与 ``provider`` 供测试断言使用。
    """
    rsa_manager = RSAKeyManager(
        private_key_path=private_key_path,
        public_key_path=public_key_path,
    )
    rsa_manager.load_or_generate()

    provider = BangumiOAuthProvider(
        base_url=base_url or issuer,
        rsa_manager=rsa_manager,
        issuer=issuer,
        audience=audience,
        token_expiry_seconds=token_expiry_seconds,
        auth_enabled=auth_enabled,
        auth_username=auth_username,
    )

    app = create_mcp_app(provider=provider)

    # 供测试断言：公钥验签与直接操作 provider 状态。
    app.state.public_key_pem = rsa_manager.get_public_key_pem()
    app.state.provider = provider

    return app
