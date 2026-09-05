"""
依赖注入模块
"""

import os
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..core.mcp_auth import PublicKeyNotFoundError, load_public_key, verify_jwt
from ..core.security import security_manager

# Bearer token认证
security = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# MCP 内部 API 鉴权配置（可通过 monkeypatch/测试注入覆盖）
# ---------------------------------------------------------------------------

_MCP_PUBLIC_KEY_PATH: str = "/mcp_auth/mcp_public.pem"
_MCP_AUDIENCE: str = "bs"
_MCP_ISSUER: str = os.environ.get("MCP_ISSUER", "http://localhost:3000")


def _split_scope(scope_claim: Any) -> list[str]:
    """将 JWT scope claim（空格分隔字符串或 list）拆分为 list。"""
    if isinstance(scope_claim, list):
        return scope_claim
    if isinstance(scope_claim, str):
        return scope_claim.split()
    return []


def get_mcp_client(
    require_scope: str = "",
):
    """返回一个 FastAPI 依赖函数，对 MCP 内部 API 进行 JWT 鉴权。

    用法::

        @app.get("/api/mcp/endpoint")
        async def handler(user=Depends(get_mcp_client())):
            ...

        @app.post("/api/mcp/endpoint")
        async def handler(user=Depends(get_mcp_client(require_scope="write"))):
            ...

    返回:
        dict: {"username": <sub>, "scope": [...], "mcp": True}
    """

    async def _dependency(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    ) -> dict[str, Any]:
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未提供认证令牌",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = credentials.credentials

        # 预加载公钥：让 PublicKeyNotFoundError 直接抛出（503），
        # 而非被 verify_jwt 内部吞掉后统一返回 None。
        try:
            load_public_key(_MCP_PUBLIC_KEY_PATH)
        except PublicKeyNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="MCP 公钥不可用，请联系运维检查服务配置",
            )

        claims = verify_jwt(
            token,
            public_key_path=_MCP_PUBLIC_KEY_PATH,
            audience=_MCP_AUDIENCE,
            issuer=_MCP_ISSUER,
        )

        if claims is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的认证令牌",
                headers={"WWW-Authenticate": "Bearer"},
            )

        scope_list = _split_scope(claims.get("scope", ""))

        # scope 校验
        if require_scope and require_scope not in scope_list:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"缺少所需权限: {require_scope}",
            )

        return {
            "username": claims.get("sub", ""),
            "scope": scope_list,
            "mcp": True,
        }

    return _dependency


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict[str, Any]:
    """获取当前用户（用于依赖注入）"""
    auth_config = security_manager.get_auth_config()

    # 如果认证被禁用，直接通过
    if not auth_config["enabled"]:
        return {"username": "admin", "auth_disabled": True}

    # 清理过期会话
    security_manager.cleanup_expired_sessions()

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    session = security_manager.validate_session(credentials.credentials)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return session


def get_current_user_from_cookie(request: Request) -> dict[str, Any]:
    """从Cookie获取当前用户（用于Web页面）"""
    auth_config = security_manager.get_auth_config()

    # 如果认证被禁用，直接通过
    if not auth_config["enabled"]:
        return {"username": "admin", "auth_disabled": True}

    # 清理过期会话
    security_manager.cleanup_expired_sessions()

    token = request.cookies.get("session_token")
    if not token:
        return None

    session = security_manager.validate_session(token)
    return session


async def get_current_user_flexible(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict[str, Any]:
    """灵活的用户认证（支持Cookie和Bearer token）"""
    auth_config = security_manager.get_auth_config()

    # 如果认证被禁用，直接通过
    if not auth_config["enabled"]:
        return {"username": "admin", "auth_disabled": True}

    # 清理过期会话
    security_manager.cleanup_expired_sessions()

    # 首先尝试从Cookie获取（Web界面）
    token = request.cookies.get("session_token")
    if token:
        session = security_manager.validate_session(token)
        if session:
            return session

    # 然后尝试从Bearer token获取（API调用）
    if credentials:
        session = security_manager.validate_session(credentials.credentials)
        if session:
            return session

    # 如果都没有有效的认证信息
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未提供有效的认证信息",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    """已登录返回会话；未登录返回 None。认证关闭时视为已登录（与 flexible 一致）。"""
    auth_config = security_manager.get_auth_config()

    if not auth_config["enabled"]:
        return {"username": "admin", "auth_disabled": True}

    security_manager.cleanup_expired_sessions()

    token = request.cookies.get("session_token")
    if token:
        session = security_manager.validate_session(token)
        if session:
            return session

    if credentials:
        session = security_manager.validate_session(credentials.credentials)
        if session:
            return session

    return None
