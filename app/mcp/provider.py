"""Bangumi-syncer 的 FastMCP OAuthProvider 实现。

实现内容：
- RSA 密钥管理（RS256 JWT 签名）
- OAuthProvider（FastMCP 4），含 authorize/token/register
- auth.enabled 分支（通过 security_manager 校验 BS 会话，或使用配置的用户名）
- consent 页面（允许/拒绝），带 CSRF 保护
- Dynamic Client Registration（RFC 7591）
- CIMD（Client ID Metadata Document）集成
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth import OAuthProvider
from fastmcp.server.auth.cimd import CIMDClientManager
from fastmcp.server.auth.redirect_validation import is_loopback_host
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from app.core.security import security_manager

logger = logging.getLogger(__name__)

# 常量
MAX_CLIENTS = 1000
PENDING_AUTH_TTL = 600  # 10 分钟
AUTH_CODE_TTL = 300  # 5 分钟
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 天

# 客户端允许的 scope 全集（校验用），与 ClientRegistrationOptions.valid_scopes 对齐。
# CIMD 合成 client 用它作为允许集：MCP SDK 的 validate_scope() 会据此校验请求 scope，
# 因此请求 "read write" 必须落在允许集内，否则报 invalid_scope。
# 注意这不改变发放策略——客户端未显式请求 scope 时仍按 _default_scopes 只发 "read"。
_ALLOWED_SCOPES = ("read", "write")


class RSAKeyManager:
    """管理用于 JWT 签名（RS256）的 RSA 密钥对。"""

    def __init__(
        self,
        private_key_path: str,
        public_key_path: str,
    ) -> None:
        self.private_key_path = private_key_path
        self.public_key_path = public_key_path
        self._private_key: rsa.RSAPrivateKey | None = None
        self._public_key: rsa.RSAPublicKey | None = None

    def generate_keys(self) -> None:
        """生成新的 RSA 密钥对并保存到磁盘。"""
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        self._private_key = private_key
        self._public_key = private_key.public_key()

        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # 确保目录存在
        os.makedirs(os.path.dirname(self.private_key_path) or ".", exist_ok=True)

        with open(self.private_key_path, "wb") as f:
            f.write(private_pem)
        # 将私钥权限设为仅属主可读写（chmod 600）
        os.chmod(self.private_key_path, 0o600)
        self._write_public_key()

    def load_or_generate(self) -> None:
        """从磁盘加载已有密钥；未找到则生成新密钥对。

        若仅存在私钥，则会从该私钥重新派生公钥（私钥绝不会被替换）。只有私钥
        缺失才会触发生成新密钥对，因此意外删除公钥不会使此前已签发的 JWT
        失效；只要私钥仍在，公钥就能随时按需重建，既有 token 的有效性
        不受影响。
        """
        if os.path.exists(self.private_key_path):
            self._load_private_key()
            self._ensure_private_key_permissions()
            if os.path.exists(self.public_key_path):
                self._load_public_key()
            else:
                logger.warning(
                    "Public key %s missing; re-deriving it from private key %s",
                    self.public_key_path,
                    self.private_key_path,
                )
                self._write_public_key()
        else:
            logger.info(
                "Private key %s not found; generating new RSA key pair",
                self.private_key_path,
            )
            self.generate_keys()

    def _load_private_key(self) -> None:
        """从磁盘加载私钥。"""
        with open(self.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )

    def _load_public_key(self) -> None:
        """从磁盘加载公钥。"""
        with open(self.public_key_path, "rb") as f:
            self._public_key = serialization.load_pem_public_key(f.read())

    def _ensure_private_key_permissions(self) -> None:
        """对已有私钥强制设置仅属主（0o600）权限。"""
        current_mode = os.stat(self.private_key_path).st_mode & 0o777
        if current_mode != 0o600:
            logger.warning(
                "Private key %s had mode 0o%o; enforcing owner-only 0o600",
                self.private_key_path,
                current_mode,
            )
            os.chmod(self.private_key_path, 0o600)

    def _write_public_key(self) -> None:
        """从已加载的私钥派生公钥并持久化。"""
        self._public_key = self.private_key.public_key()
        public_pem = self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        os.makedirs(os.path.dirname(self.public_key_path) or ".", exist_ok=True)
        with open(self.public_key_path, "wb") as f:
            f.write(public_pem)

    @property
    def private_key(self) -> rsa.RSAPrivateKey:
        if self._private_key is None:
            raise RuntimeError("Keys not loaded. Call load_or_generate() first.")
        return self._private_key

    @property
    def public_key(self) -> rsa.RSAPublicKey:
        if self._public_key is None:
            raise RuntimeError("Keys not loaded. Call load_or_generate() first.")
        return self._public_key

    def get_private_key_pem(self) -> bytes:
        """获取 PEM 格式的私钥。"""
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def get_public_key_pem(self) -> bytes:
        """获取 PEM 格式的公钥。"""
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def sign_jwt(self, claims: dict[str, Any]) -> str:
        """用 RS256 对 JWT 签名；若缺失 jti 则补充以保证唯一性。"""
        if "jti" not in claims:
            claims["jti"] = secrets.token_urlsafe(16)
        return jwt.encode(claims, self.get_private_key_pem(), algorithm="RS256")

    def verify_jwt(
        self, token: str, audience: str | None = None
    ) -> dict[str, Any] | None:
        """用公钥验证 JWT，返回 claims；无效则返回 None。"""
        try:
            return jwt.decode(
                token,
                self.get_public_key_pem(),
                algorithms=["RS256"],
                audience=audience,
                options={"verify_aud": audience is not None},
            )
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return None


class BangumiOAuthProvider(OAuthProvider):
    """Bangumi-syncer 的 OAuth 授权服务器 Provider。

    基于 FastMCP 4 OAuthProvider 实现：
    - 通过 CIMDClientManager 支持 CIMD（Client ID Metadata Document）
    - auth.enabled 分支（校验 BS 会话，或使用配置的用户名）
    - 带 CSRF 保护的 consent 页面流程
    - 签发 JWT token（RS256）
    - Dynamic Client Registration（RFC 7591）
    """

    def __init__(
        self,
        *,
        base_url: str,
        rsa_manager: RSAKeyManager,
        issuer: str,
        audience: str,
        token_expiry_seconds: int = 3600,
        auth_enabled: bool = False,
        auth_username: str = "admin",
        refresh_token_ttl: int = REFRESH_TOKEN_TTL,
        client_registration_options: ClientRegistrationOptions | None = None,
        revocation_options: RevocationOptions | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            issuer_url=issuer,
            client_registration_options=client_registration_options
            or ClientRegistrationOptions(
                enabled=True, valid_scopes=list(_ALLOWED_SCOPES)
            ),
            revocation_options=revocation_options or RevocationOptions(enabled=True),
        )
        self.rsa_manager = rsa_manager
        self.issuer = issuer
        self.audience = audience
        self.token_expiry_seconds = token_expiry_seconds
        self.refresh_token_ttl = refresh_token_ttl
        self.auth_enabled = auth_enabled
        self.auth_username = auth_username

        # 实际发放的默认 scope：客户端不请求 scope 时只给 read（安全默认）。
        # 与 _ALLOWED_SCOPES（校验允许集）区分：后者用于通过 SDK 的 scope 校验。
        self._default_scopes = ["read"]
        self.MAX_CLIENTS = MAX_CLIENTS

        # 内存态存储
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._pending_auths: dict[str, dict[str, Any]] = {}
        # jti -> access token 的 exp，用于惰性清理吊销记录
        self._revoked_tokens: dict[str, float] = {}

        # CIMD manager：default_scope 是校验允许集而非发放默认值。
        # SDK 用 CIMD 合成 client 的 scope 校验请求 scope（如 "read write"），
        # 故这里注入完整允许集；未显式请求 scope 时 authorize() 仍发放 _default_scopes。
        self.cimd = CIMDClientManager(
            enable_cimd=True,
            default_scope=" ".join(_ALLOWED_SCOPES),
        )

    # ------------------------------------------------------------------
    # OAuthProvider 接口
    # ------------------------------------------------------------------

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """重写以注入 client_id_metadata_document_supported=True。"""
        routes = super().get_routes(mcp_path)
        # 重建 metadata 并声明支持 CIMD
        for i, route in enumerate(routes):
            if (
                isinstance(route, Route)
                and route.path == "/.well-known/oauth-authorization-server"
            ):
                metadata = build_metadata(
                    self.base_url,
                    self.service_documentation_url,
                    self.client_registration_options or ClientRegistrationOptions(),
                    self.revocation_options or RevocationOptions(),
                )
                metadata.issuer = self.issuer_url
                metadata.client_id_metadata_document_supported = True
                metadata_handler = MetadataHandler(metadata)
                routes[i] = Route(
                    path=route.path,
                    endpoint=cors_middleware(
                        metadata_handler.handle, ["GET", "OPTIONS"]
                    ),
                    methods=route.methods or ["GET", "OPTIONS"],
                    name=route.name,
                    include_in_schema=route.include_in_schema,
                )
                break
        return routes

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """按 ID 获取 client。

        CIMD 分支：URL 形式的 client_id → CIMDClientManager
        DCR 分支：静态注册表
        """
        # CIMD 分支：URL 形式的 client_id
        if self.cimd.is_cimd_client_id(client_id):
            return await self.cimd.get_client(client_id)
        # DCR 回退：静态注册表
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """注册新 client（DCR），并强制执行 client 数量上限。"""
        if not client_info.client_id:
            client_info.client_id = secrets.token_urlsafe(16)
        if (
            client_info.token_endpoint_auth_method != "none"
            and not client_info.client_secret
        ):
            client_info.client_secret = secrets.token_hex(32)
        # 未指定 scope 时分配默认 scope
        if not client_info.scope:
            client_info.scope = " ".join(self._default_scopes)
        # 强制执行 client 数量上限
        if len(self._clients) >= self.MAX_CLIENTS:
            raise RegistrationError(
                f"Client registration limit reached ({self.MAX_CLIENTS}). "
                "Cannot register new clients."
            )
        self._clients[client_info.client_id] = client_info

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """处理 /authorize：暂存待处理请求并返回 consent URL。

        校验 client 已注册且 redirect_uri 匹配。
        """
        # 校验 client 已注册
        client_id = client.client_id
        if self.cimd.is_cimd_client_id(client_id):
            # CIMD client 通过 CIMD 校验，跳过注册表检查
            pass
        elif client_id not in self._clients:
            raise AuthorizeError(
                error="unauthorized_client",
                error_description=f"Unknown client: {client_id}",
            )

        # 校验 redirect_uri
        redirect_uri_str = str(params.redirect_uri)
        if params.redirect_uri_provided_explicitly:
            if self.cimd.is_cimd_client_id(client_id):
                # P0-1：CIMD client 必须按组件级匹配，用其 CIMD 文档的
                # redirect_uris 校验 redirect_uri
                cimd_client = await self.cimd.get_client(client_id)
                if (
                    cimd_client is not None
                    and hasattr(cimd_client, "cimd_document")
                    and cimd_client.cimd_document is not None
                ):
                    # validate_redirect_uri 位于 CIMDFetcher（self.cimd._fetcher）
                    if not self.cimd._fetcher.validate_redirect_uri(
                        cimd_client.cimd_document, redirect_uri_str
                    ):
                        raise AuthorizeError(
                            error="invalid_request",
                            error_description=(
                                f"redirect_uri mismatch: {redirect_uri_str} "
                                "not in CIMD document redirect_uris"
                            ),
                        )
                else:
                    raise AuthorizeError(
                        error="invalid_request",
                        error_description=(
                            "redirect_uri validation failed: cannot resolve "
                            f"CIMD document for {client_id}"
                        ),
                    )
            elif client_id in self._clients:
                # P0-2：DCR client 使用组件级精确匹配
                registered_client = self._clients[client_id]
                allowed_uris = [str(u) for u in (registered_client.redirect_uris or [])]
                if allowed_uris and not self._matches_redirect_uri(
                    redirect_uri_str, allowed_uris
                ):
                    raise AuthorizeError(
                        error="invalid_request",
                        error_description=(
                            f"redirect_uri mismatch: {redirect_uri_str} "
                            "not in registered URIs"
                        ),
                    )

        # 触发过期内存态数据的惰性清理，避免无限制增长
        self._cleanup_expired_state()
        request_token = secrets.token_urlsafe(32)
        # 生成与该待处理 auth 绑定的 CSRF token
        csrf_token = secrets.token_urlsafe(32)
        self._pending_auths[request_token] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri_str,
            "scopes": params.scopes or self._default_scopes,
            "state": params.state,
            "code_challenge": params.code_challenge,
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
            "csrf_token": csrf_token,
            "created_at": time.time(),
        }
        return f"/consent?request_token={request_token}"

    @staticmethod
    def _matches_redirect_uri(redirect_uri: str, allowed_uris: list[str]) -> bool:
        """redirect URI 的组件级精确匹配。

        比较 (scheme, netloc, path) 各组成部分。对 loopback 主机
        （localhost/127.0.0.1），按 RFC 8252 §7.3 允许端口灵活变化。
        """
        parsed = urlsplit(redirect_uri)
        for allowed in allowed_uris:
            allowed_parsed = urlsplit(allowed)
            # scheme 必须完全匹配
            if parsed.scheme != allowed_parsed.scheme:
                continue
            # path 必须完全匹配
            if parsed.path.rstrip("/") != allowed_parsed.path.rstrip("/"):
                continue
            # host 必须完全匹配
            if parsed.hostname != allowed_parsed.hostname:
                continue
            # 端口：仅对 loopback 主机允许灵活变化
            if is_loopback_host(parsed.hostname):
                return True
            # 非 loopback：端口必须完全匹配
            if parsed.port == allowed_parsed.port:
                return True
        return False

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        """按字符串加载 authorization code。"""
        return self._auth_codes.get(authorization_code)

    def _build_jwt_claims(
        self,
        subject: str,
        scopes: list[str],
        resource: str | None = None,
        client_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """构建 JWT claims 并签名，返回 (claims, access_token)。

        client_id 写入 JWT claims，使 load_access_token 可回填到 AccessToken.client_id，
        从而让 SDK RevocationHandler 的 ``token.client_id == client.client_id`` 门槛成立。
        """
        now = int(time.time())
        expires_at = now + self.token_expiry_seconds
        claims: dict[str, Any] = {
            "sub": subject,
            "scope": " ".join(scopes),
            "iss": self.issuer,
            "aud": self.audience,
            "iat": now,
            "exp": expires_at,
        }
        if resource:
            claims["resource"] = resource
        if client_id:
            claims["client_id"] = client_id
        return claims, self.rsa_manager.sign_jwt(claims)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """用 authorization code 换取 access token + refresh token。"""
        self._cleanup_expired_state()
        _, access_token = self._build_jwt_claims(
            subject=authorization_code.subject or self.auth_username,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
            client_id=client.client_id,
        )
        refresh_token_str = secrets.token_urlsafe(32)

        # 存储 refresh token
        refresh_token = RefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + self.refresh_token_ttl,
            subject=authorization_code.subject,
        )
        self._refresh_tokens[refresh_token_str] = refresh_token

        # 清理 auth code（一次性使用）
        self._auth_codes.pop(authorization_code.code, None)

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=self.token_expiry_seconds,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh_token_str,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """验证 JWT access token 并返回 AccessToken。

        同时对照吊销集合检查 token 的 jti（P1-2）。
        """
        claims = self.rsa_manager.verify_jwt(token, audience=self.audience)
        if claims is None:
            return None

        # P1-2：检查该 token 是否已被吊销
        jti = claims.get("jti")
        if jti and jti in self._revoked_tokens:
            return None

        return AccessToken(
            token=token,
            client_id=claims.get("client_id", ""),
            scopes=claims.get("scope", "").split(),
            expires_at=claims.get("exp"),
            subject=claims.get("sub"),
            claims=claims,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """按字符串加载 refresh token。"""
        token_obj = self._refresh_tokens.get(refresh_token)
        if token_obj is None or token_obj.client_id != client.client_id:
            return None
        return token_obj

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """用 refresh token 换取新的 access token + refresh token（轮换）。"""
        self._cleanup_expired_state()
        _, access_token = self._build_jwt_claims(
            subject=refresh_token.subject or self.auth_username,
            scopes=scopes,
            client_id=client.client_id,
        )

        # 轮换 refresh token（生成新的）。每次轮换都会重置过期时间
        # （滑动窗口）：活跃使用的会话永不过期。
        new_refresh_token_str = secrets.token_urlsafe(32)
        new_refresh_token = RefreshToken(
            token=new_refresh_token_str,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=int(time.time()) + self.refresh_token_ttl,
            subject=refresh_token.subject,
        )
        self._refresh_tokens[new_refresh_token_str] = new_refresh_token

        # 移除旧的 refresh token
        self._refresh_tokens.pop(refresh_token.token, None)

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=self.token_expiry_seconds,
            scope=" ".join(scopes),
            refresh_token=new_refresh_token_str,
        )

    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
    ) -> None:
        """吊销 access token 或 refresh token。

        对 AccessToken：在吊销表中记录 jti（及其 exp）（P1-2）。
        对 RefreshToken：从 refresh token 存储中移除。
        """
        self._cleanup_expired_state()
        if isinstance(token, AccessToken):
            # P1-2：在吊销集合中记录 jti，使 verify_token 拒绝该 token。
            # 同时存储该 token 的 exp，以便之后惰性清理该记录。
            jti = token.claims.get("jti")
            if jti:
                self._revoked_tokens[jti] = float(
                    token.expires_at or (time.time() + self.token_expiry_seconds)
                )
        elif isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)

    # ------------------------------------------------------------------
    # consent 流程辅助方法
    # ------------------------------------------------------------------

    def _cleanup_expired_state(self) -> None:
        """移除过期的内存态数据（惰性 TTL 强制）。

        清理四个存储，避免未被认领/已过期的条目持续累积：
        - 待处理 auth 请求：创建后经过 PENDING_AUTH_TTL
        - authorization code：超过其 ``expires_at`` 后
        - refresh token：超过其 ``expires_at`` 后（None = 永不过期）
        - 已吊销 jti 记录：超过该 access token 的 exp 后
        """
        now = time.time()
        expired_pending = [
            rt
            for rt, info in self._pending_auths.items()
            if now - info.get("created_at", 0) > PENDING_AUTH_TTL
        ]
        for rt in expired_pending:
            del self._pending_auths[rt]

        expired_codes = [
            code for code, obj in self._auth_codes.items() if obj.expires_at < now
        ]
        for code in expired_codes:
            del self._auth_codes[code]

        expired_refresh = [
            token
            for token, obj in self._refresh_tokens.items()
            if obj.expires_at is not None and obj.expires_at < now
        ]
        for token in expired_refresh:
            del self._refresh_tokens[token]

        expired_revoked = [
            jti for jti, exp in self._revoked_tokens.items() if exp < now
        ]
        for jti in expired_revoked:
            del self._revoked_tokens[jti]

        logger.debug(
            "Cleaned in-memory OAuth state: pending=%d auth_codes=%d "
            "refresh_tokens=%d revoked=%d",
            len(expired_pending),
            len(expired_codes),
            len(expired_refresh),
            len(expired_revoked),
        )

    async def get_consent_context(
        self, request_token: str, session_token: str | None = None
    ) -> dict[str, Any] | None:
        """获取 consent 页面所需的上下文。

        Args:
            request_token: 待处理 auth 请求的 token。
            session_token: 可选的 session token，用于通过 security_manager 校验。
        """
        self._cleanup_expired_state()
        pending = self._pending_auths.get(request_token)
        if pending is None:
            return None

        # 依据 auth.enabled 决定用户名
        username = self.auth_username
        if self.auth_enabled:
            # 通过 security_manager 校验 BS 会话（同进程，非 HTTP）
            if session_token:
                session = security_manager.validate_session(session_token)
                if session:
                    username = session.get("username", self.auth_username)

        return {
            "request_token": request_token,
            "client_id": pending["client_id"],
            "scopes": pending["scopes"],
            "username": username,
            "auth_enabled": self.auth_enabled,
            "csrf_token": pending.get("csrf_token", ""),
        }

    async def handle_consent_allow(
        self,
        request_token: str,
        username: str | None = None,
        csrf_token: str | None = None,
    ) -> str:
        """处理 consent 允许操作：校验 CSRF 并生成 authorization code。

        Args:
            request_token: 待处理 auth 请求的 token。
            username: 可选的用户名覆盖值。
            csrf_token: 表单提交中的 CSRF token。
        """
        self._cleanup_expired_state()
        pending_info = self._pending_auths.get(request_token)
        if pending_info is None:
            raise ValueError("Invalid or expired request_token")

        # 校验 CSRF token（一次性使用，绑定到该待处理 auth）
        expected_csrf = pending_info.get("csrf_token", "")
        if not csrf_token or not hmac.compare_digest(
            str(csrf_token), str(expected_csrf)
        ):
            raise ValueError("Invalid CSRF token")

        auth_code = secrets.token_urlsafe(32)
        code_obj = AuthorizationCode(
            code=auth_code,
            scopes=pending_info["scopes"],
            expires_at=time.time() + AUTH_CODE_TTL,  # 5 分钟过期
            client_id=pending_info["client_id"],
            code_challenge=pending_info["code_challenge"],
            redirect_uri=AnyUrl(pending_info["redirect_uri"]),
            redirect_uri_provided_explicitly=pending_info[
                "redirect_uri_provided_explicitly"
            ],
            resource=pending_info.get("resource"),
            subject=username or self.auth_username,
        )
        self._auth_codes[auth_code] = code_obj

        # 清理待处理请求
        del self._pending_auths[request_token]

        return auth_code

    async def handle_consent_deny(self, request_token: str) -> None:
        """处理 consent 拒绝操作：清理待处理请求。"""
        self._pending_auths.pop(request_token, None)

    def _render_consent_form(self, context: dict[str, Any]) -> str:
        """渲染 HTML consent 表单，进行恰当的 HTML 转义（防止 XSS）。"""
        # 转义所有动态字段以防止 XSS
        request_token = html.escape(str(context["request_token"]))
        client_id = html.escape(str(context["client_id"]))
        scopes = context.get("scopes", [])
        username = html.escape(str(context.get("username", "unknown")))
        csrf_token = html.escape(str(context.get("csrf_token", "")))

        return f"""
        <!DOCTYPE html>
        <html>
        <head><title>Authorize {client_id}</title></head>
        <body>
            <h1>Authorization Request</h1>
            <p>Client <strong>{client_id}</strong> is requesting access.</p>
            <p>User: <strong>{username}</strong></p>
            <p>Scopes: <strong>{html.escape(", ".join(scopes))}</strong></p>
            <form method="POST" action="/consent">
                <input type="hidden" name="request_token" value="{request_token}">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <button type="submit" name="action" value="allow">Allow</button>
                <button type="submit" name="action" value="deny">Deny</button>
            </form>
        </body>
        </html>
        """


# ------------------------------------------------------------------
# server 工厂
# ------------------------------------------------------------------


def create_auth_server(
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
    """创建一个完整的、启用 auth 的 Starlette app，用于测试。

    返回带 OAuth 端点（authorize、token、register）和 consent 页面的
    Starlette app。
    """
    from fastmcp import FastMCP
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

    # 初始化 RSA 密钥
    rsa_manager = RSAKeyManager(
        private_key_path=private_key_path,
        public_key_path=public_key_path,
    )
    rsa_manager.load_or_generate()

    # 创建 provider
    provider = BangumiOAuthProvider(
        base_url=base_url or issuer,
        rsa_manager=rsa_manager,
        issuer=issuer,
        audience=audience,
        token_expiry_seconds=token_expiry_seconds,
        auth_enabled=auth_enabled,
        auth_username=auth_username,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=list(_ALLOWED_SCOPES)
        ),
        revocation_options=RevocationOptions(enabled=True),
    )

    # 创建带 auth 的 MCP server
    mcp = FastMCP(name="bangumi-syncer-mcp", auth=provider)

    # 在调用 http_app 之前注册 consent 路由
    @mcp.custom_route("/consent", methods=["GET", "POST"])
    async def consent_route(request: Request) -> Response:
        return await handle_consent(request, provider)

    # 获取 Starlette app
    app = mcp.http_app(path="/mcp")

    # 将公钥和 provider 存入 app.state 供测试使用
    app.state.public_key_pem = rsa_manager.get_public_key_pem()
    app.state.provider = provider

    return app


async def handle_consent(request: Request, provider: BangumiOAuthProvider) -> Response:
    """处理 consent 页面的 GET/POST，可作为 custom_route 处理器使用。"""
    # 从 query（GET）或 form（POST）获取 request_token
    if request.method == "GET":
        request_token = request.query_params.get("request_token")
    else:
        form = await request.form()
        request_token = form.get("request_token")

    if not request_token:
        return HTMLResponse("<h1>Error: missing request_token</h1>", status_code=400)

    # 从 cookie 中提取 session token
    session_token = None
    cookie = request.headers.get("cookie")
    if cookie:
        # 解析 cookie 以查找 session token
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("session_token="):
                session_token = part.split("=", 1)[1]
                break

    if request.method == "GET":
        # 当 auth.enabled=True 时先校验会话
        if provider.auth_enabled:
            if session_token:
                session = security_manager.validate_session(session_token)
                if not session:
                    return HTMLResponse(
                        "<h1>Error: session invalid or expired. Please log in.</h1>",
                        status_code=401,
                    )
            else:
                return HTMLResponse(
                    "<h1>Error: no session token. Please log in.</h1>",
                    status_code=401,
                )

        context = await provider.get_consent_context(
            request_token, session_token=session_token
        )
        if context is None:
            return HTMLResponse(
                "<h1>Error: invalid or expired request</h1>", status_code=400
            )
        return HTMLResponse(provider._render_consent_form(context))

    # POST：校验 CSRF token
    form = await request.form()
    action = form.get("action", "deny")
    rt = str(request_token)
    submitted_csrf = form.get("csrf_token")

    # 在任何删除操作之前查找待处理 auth
    pending_info = provider._pending_auths.get(rt)
    if pending_info is None:
        return HTMLResponse(
            "<h1>Error: invalid or expired request</h1>", status_code=400
        )

    # 校验 CSRF token（一次性使用，绑定到该待处理 auth）
    expected_csrf = pending_info.get("csrf_token", "")
    if not submitted_csrf or not hmac.compare_digest(
        str(submitted_csrf), str(expected_csrf)
    ):
        return HTMLResponse("<h1>Error: invalid CSRF token</h1>", status_code=403)

    redirect_uri = pending_info["redirect_uri"]
    state = pending_info.get("state")

    if action == "allow":
        # 当 auth_enabled=True 时，在 POST 允许时重新校验会话
        username = provider.auth_username
        if provider.auth_enabled:
            if session_token:
                session = security_manager.validate_session(session_token)
                if not session:
                    return HTMLResponse(
                        "<h1>Error: BS session expired or invalid</h1>",
                        status_code=401,
                    )
                username = session.get("username", provider.auth_username)
            else:
                return HTMLResponse(
                    "<h1>Error: no session token</h1>",
                    status_code=401,
                )
        auth_code = await provider.handle_consent_allow(
            rt, username=username, csrf_token=submitted_csrf
        )
        params: dict[str, str] = {"code": auth_code}
        if state:
            params["state"] = state
        return RedirectResponse(
            url=f"{redirect_uri}?{urlencode(params)}", status_code=302
        )
    else:
        await provider.handle_consent_deny(rt)
        params = {
            "error": "access_denied",
            "error_description": "User denied authorization",
        }
        if state:
            params["state"] = state
        return RedirectResponse(
            url=f"{redirect_uri}?{urlencode(params)}", status_code=302
        )
