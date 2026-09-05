"""
MCP JWT 验签模块

为 BS 侧提供 RS256 JWT 验签能力，供 MCP 内部 API 使用。
MCP 授权服务器用 RS256 私钥签发 JWT（claims: sub, scope, exp, iss, aud），
BS 持有公钥验签。
"""

from __future__ import annotations

import json
import time
from base64 import urlsafe_b64decode
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .logging import logger

# ---------------------------------------------------------------------------
# 异常定义
# ---------------------------------------------------------------------------


class McpAuthError(Exception):
    """MCP 认证相关异常的基类。"""


class PublicKeyNotFoundError(McpAuthError):
    """公钥文件缺失或无法加载。"""


# ---------------------------------------------------------------------------
# 公钥加载与缓存
# ---------------------------------------------------------------------------

# 内存缓存：路径 -> (公钥对象, 文件 mtime)
_public_key_cache: dict[str, tuple[Any, float]] = {}


def _base64url_decode(s: str) -> bytes:
    """Base64URL 解码（无填充）。"""
    padding_needed = 4 - len(s) % 4
    if padding_needed != 4:
        s += "=" * padding_needed
    return urlsafe_b64decode(s)


def load_public_key(public_key_path: str) -> Any:
    """从 PEM 文件加载 RSA 公钥并缓存。

    若缓存中文件未变更则直接返回缓存副本。
    文件缺失时抛出 PublicKeyNotFoundError。
    """
    # 优先检查缓存
    cached = _get_cached_public_key(public_key_path)
    if cached is not None:
        return cached

    path = Path(public_key_path)
    if not path.exists():
        raise PublicKeyNotFoundError(f"公钥文件不存在: {public_key_path}")

    pem_data = path.read_bytes()
    try:
        public_key = serialization.load_pem_public_key(pem_data)
    except (ValueError, TypeError) as e:
        raise McpAuthError(f"公钥文件解析失败: {e}") from e

    # 缓存公钥及其 mtime
    mtime = path.stat().st_mtime
    _public_key_cache[public_key_path] = (public_key, mtime)
    return public_key


def _get_cached_public_key(public_key_path: str) -> Any | None:
    """从缓存获取公钥，若文件已变更则返回 None。"""
    cached = _public_key_cache.get(public_key_path)
    if cached is None:
        return None

    key, cached_mtime = cached
    try:
        current_mtime = Path(public_key_path).stat().st_mtime
    except OSError:
        return None

    if current_mtime != cached_mtime:
        return None
    return key


def reload_public_key(public_key_path: str) -> Any:
    """强制重新加载公钥（忽略缓存）。"""
    _public_key_cache.pop(public_key_path, None)
    return load_public_key(public_key_path)


def _get_public_key(public_key_path: str) -> Any:
    """获取公钥：优先缓存，否则从文件加载。"""
    key = _get_cached_public_key(public_key_path)
    if key is not None:
        return key
    return load_public_key(public_key_path)


# ---------------------------------------------------------------------------
# JWT 验签
# ---------------------------------------------------------------------------


def verify_jwt(
    token: str | None,
    public_key_path: str,
    audience: str,
    issuer: str | None = None,
) -> dict[str, Any] | None:
    """验证 RS256 JWT。

    参数:
        token: JWT 字符串
        public_key_path: PEM 公钥文件路径
        audience: 期望的 aud 值（必须存在且匹配）
        issuer: 期望的 iss 值（可选；提供时必须存在且匹配）

    返回:
        验签通过且未过期时返回 claims dict，否则返回 None。
    """
    if not token:
        return None

    try:
        # 解析 JWT 结构
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, signature_b64 = parts

        # 解码 payload 以检查 exp/aud/iss（无需验签即可读）
        try:
            payload_bytes = _base64url_decode(payload_b64)
            claims = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

        # 检查 exp
        exp = claims.get("exp")
        if exp is not None and time.time() >= exp:
            logger.debug("JWT 已过期")
            return None

        # 检查 aud：必须存在且等于期望 audience
        aud = claims.get("aud")
        if aud is None or aud != audience:
            logger.debug(f"JWT audience 无效: 期望 {audience}, 实际 {aud}")
            return None

        # 检查 iss（可选）：提供时必须存在且匹配
        if issuer is not None:
            iss = claims.get("iss")
            if iss is None or iss != issuer:
                logger.debug(f"JWT issuer 无效: 期望 {issuer}, 实际 {iss}")
                return None

        # 获取公钥
        try:
            public_key = _get_public_key(public_key_path)
        except McpAuthError:
            logger.error("JWT 验签失败：无法加载公钥")
            return None

        # 验签
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        try:
            signature = _base64url_decode(signature_b64)
        except ValueError:
            return None

        try:
            public_key.verify(
                signature,
                signing_input,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception:
            logger.debug("JWT 签名验证失败")
            return None

        return claims

    except Exception as e:
        logger.debug(f"JWT 验证异常: {e}")
        return None
