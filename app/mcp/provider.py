"""
FastMCP OAuthProvider 占位实现

T1 骨架阶段：继承 OAuthProvider，实现全部抽象方法的最小版本。
方法签名与 FastMCP 4 一致，行为为占位（抛 NotImplementedError 或返回 None）。
完整业务逻辑（CIMD、DCR、authorize 流程）在 T3-T5 实现。
"""

from fastmcp.server.auth import OAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


class PlaceholderOAuthProvider(OAuthProvider):
    """最小 OAuthProvider 子类占位，仅保证 FastMCP 实例化与路由注册正常。

    所有方法均为最小实现：
    - get_client: 返回 None（无已知客户端）
    - register_client: 抛 NotImplementedError（DCR 未实现）
    - authorize: 抛 NotImplementedError（授权流程未实现）
    - load_authorization_code: 返回 None
    - exchange_authorization_code: 抛 TokenError
    - load_refresh_token: 返回 None
    - exchange_refresh_token: 抛 TokenError
    - load_access_token: 返回 None
    - revoke_token: noop
    - exchange_identity_assertion: 抛 TokenError（SEP-990 可选）
    """

    def __init__(self, *, base_url: str, **kwargs) -> None:
        super().__init__(
            base_url=base_url,
            client_registration_options=kwargs.pop("client_registration_options", None),
            revocation_options=kwargs.pop("revocation_options", None),
            **kwargs,
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """T3 实现：CIMD 检测 + DCR 注册表查询。当前返回 None。"""
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """T5 实现：DCR 注册。当前拒绝。"""
        raise NotImplementedError("DCR not implemented in T1 skeleton")

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """T3 实现：授权码流程。当前拒绝。"""
        raise NotImplementedError("authorize not implemented in T1 skeleton")

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """T3 实现：从存储加载授权码。当前返回 None。"""
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """T3 实现：授权码换 token。当前拒绝。"""
        raise TokenError("invalid_grant", "authorization code exchange not implemented")

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """T3 实现：加载 refresh token。当前返回 None。"""
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """T3 实现：refresh token 换 access token。当前拒绝。"""
        raise TokenError("invalid_grant", "refresh token exchange not implemented")

    async def load_access_token(self, token: str) -> AccessToken | None:
        """T3 实现：验证 access token。当前返回 None（全部拒绝）。"""
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """T3 实现：撤销 token。当前 noop。"""
        pass
