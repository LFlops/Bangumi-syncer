"""MCP 内部 API 鉴权依赖 get_mcp_client 测试。"""

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.api import deps

# ---------------------------------------------------------------------------
# Helpers: RSA key pair + JWT signing (mirrors tests/core/test_mcp_auth.py)
# ---------------------------------------------------------------------------


def _generate_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _public_key_pem(public_key) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _base64url_encode(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(s: str) -> bytes:
    import base64

    padding_needed = 4 - len(s) % 4
    if padding_needed != 4:
        s += "=" * padding_needed
    return base64.urlsafe_b64decode(s)


def _sign(data: bytes, private_key) -> bytes:
    return private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def create_jwt(claims: dict, private_key, expires_in: int = 3600) -> str:
    import json

    header = {"alg": "RS256", "typ": "JWT"}
    now = int(time.time())
    claims_full = {**claims, "iat": now, "exp": now + expires_in}
    header_b64 = _base64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _base64url_encode(
        json.dumps(claims_full, separators=(",", ":")).encode()
    )
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = _sign(signing_input, private_key)
    signature_b64 = _base64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{signature_b64}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def key_pair():
    return _generate_key_pair()


@pytest.fixture
def other_key_pair():
    return _generate_key_pair()


@pytest.fixture
def public_key_pem_file(tmp_path, key_pair) -> Path:
    _, public_key = key_pair
    pem_path = tmp_path / "mcp_public_key.pem"
    pem_path.write_bytes(_public_key_pem(public_key))
    return pem_path


@pytest.fixture
def mcp_deps_env(public_key_pem_file):
    """Patch module-level public key path, audience and issuer for tests."""
    with (
        patch.object(deps, "_MCP_PUBLIC_KEY_PATH", str(public_key_pem_file)),
        patch.object(deps, "_MCP_AUDIENCE", "bs"),
        patch.object(deps, "_MCP_ISSUER", "http://localhost:3000"),
    ):
        yield


# ---------------------------------------------------------------------------
# Scenario 1: 有效 JWT
# ---------------------------------------------------------------------------


class TestValidJwt:
    """携带有效 JWT → 返回用户身份 dict。"""

    @pytest.mark.asyncio
    async def test_valid_jwt_returns_identity(
        self, key_pair, public_key_pem_file, mcp_deps_env
    ):
        from app.api.deps import get_mcp_client

        private_key, _ = key_pair
        token = create_jwt(
            {
                "sub": "alice",
                "scope": "read write",
                "iss": "http://localhost:3000",
                "aud": "bs",
            },
            private_key,
        )

        # 直接调用依赖函数进行精确验证
        cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        result = await get_mcp_client()(credentials=cred)

        assert result["username"] == "alice"
        assert "read" in result["scope"]
        assert "write" in result["scope"]
        assert result["mcp"] is True


# ---------------------------------------------------------------------------
# Scenario 2: 无凭证
# ---------------------------------------------------------------------------


class TestMissingCredentials:
    """无 Authorization 头 → 401。"""

    @pytest.mark.asyncio
    async def test_no_credentials_raises_401(self, mcp_deps_env):
        from app.api.deps import get_mcp_client

        with pytest.raises(HTTPException) as ei:
            await get_mcp_client()(credentials=None)
        assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 3: JWT 签名无效 / 公钥不匹配
# ---------------------------------------------------------------------------


class TestInvalidSignature:
    """由其他密钥签名的 JWT → 401，不泄露业务数据。"""

    @pytest.mark.asyncio
    async def test_wrong_key_raises_401(
        self, key_pair, other_key_pair, public_key_pem_file, mcp_deps_env
    ):
        from app.api.deps import get_mcp_client

        _, _ = key_pair
        other_private_key, _ = other_key_pair
        token = create_jwt(
            {
                "sub": "attacker",
                "scope": "admin",
                "iss": "http://localhost:3000",
                "aud": "bs",
            },
            other_private_key,
        )

        with pytest.raises(HTTPException) as ei:
            await get_mcp_client()(
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=token
                )
            )
        assert ei.value.status_code == 401
        # 不泄露业务数据
        assert "attacker" not in str(ei.value.detail)
        assert "admin" not in str(ei.value.detail)


# ---------------------------------------------------------------------------
# Scenario 4: JWT 过期
# ---------------------------------------------------------------------------


class TestExpiredJwt:
    """JWT 过期 → 401。"""

    @pytest.mark.asyncio
    async def test_expired_jwt_raises_401(
        self, key_pair, public_key_pem_file, mcp_deps_env
    ):
        from app.api.deps import get_mcp_client

        private_key, _ = key_pair
        token = create_jwt(
            {
                "sub": "alice",
                "scope": "read",
                "iss": "http://localhost:3000",
                "aud": "bs",
            },
            private_key,
            expires_in=-3600,
        )

        with pytest.raises(HTTPException) as ei:
            await get_mcp_client()(
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=token
                )
            )
        assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 5: scope 校验
# ---------------------------------------------------------------------------


class TestScopeCheck:
    """write 操作缺 write scope → 403；read 操作缺 read → 403。"""

    @pytest.mark.asyncio
    async def test_write_scope_required_but_missing_raises_403(
        self, key_pair, public_key_pem_file, mcp_deps_env
    ):
        from app.api.deps import get_mcp_client

        private_key, _ = key_pair
        # JWT 只有 read scope
        token = create_jwt(
            {
                "sub": "bob",
                "scope": "read",
                "iss": "http://localhost:3000",
                "aud": "bs",
            },
            private_key,
        )

        with pytest.raises(HTTPException) as ei:
            await get_mcp_client(require_scope="write")(
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=token
                )
            )
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_read_scope_required_but_missing_raises_403(
        self, key_pair, public_key_pem_file, mcp_deps_env
    ):
        from app.api.deps import get_mcp_client

        private_key, _ = key_pair
        # JWT 只有 write scope
        token = create_jwt(
            {
                "sub": "carol",
                "scope": "write",
                "iss": "http://localhost:3000",
                "aud": "bs",
            },
            private_key,
        )

        with pytest.raises(HTTPException) as ei:
            await get_mcp_client(require_scope="read")(
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=token
                )
            )
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_write_scope_present_succeeds(
        self, key_pair, public_key_pem_file, mcp_deps_env
    ):
        from app.api.deps import get_mcp_client

        private_key, _ = key_pair
        token = create_jwt(
            {
                "sub": "dave",
                "scope": "read write",
                "iss": "http://localhost:3000",
                "aud": "bs",
            },
            private_key,
        )

        result = await get_mcp_client(require_scope="write")(
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )
        assert result["username"] == "dave"
        assert "write" in result["scope"]


# ---------------------------------------------------------------------------
# Scenario 6: 公钥路径缺失
# ---------------------------------------------------------------------------


class TestPublicKeyMissing:
    """公钥路径缺失（PublicKeyNotFoundError）→ 明确 503 而非崩溃。"""

    @pytest.mark.asyncio
    async def test_public_key_missing_raises_503(self, key_pair, mcp_deps_env):
        from app.api.deps import get_mcp_client

        # Override to a nonexistent path for this test
        with patch.object(deps, "_MCP_PUBLIC_KEY_PATH", "/nonexistent/key.pem"):
            private_key, _ = key_pair
            token = create_jwt(
                {
                    "sub": "eve",
                    "scope": "read",
                    "iss": "http://localhost:3000",
                    "aud": "bs",
                },
                private_key,
            )

            with pytest.raises(HTTPException) as ei:
                await get_mcp_client()(
                    credentials=HTTPAuthorizationCredentials(
                        scheme="Bearer", credentials=token
                    )
                )
            # 公钥不可用应明确返回 503，且不泄露内部路径信息
            assert ei.value.status_code == 503
            assert "/nonexistent/key.pem" not in str(ei.value.detail)


# ---------------------------------------------------------------------------
# Scenario 7: iss 校验（M3）— 走 deps 真实路径
# ---------------------------------------------------------------------------


class TestIssuerValidationViaDeps:
    """iss 校验通过 get_mcp_client 真实路径执行。"""

    @pytest.mark.asyncio
    async def test_correct_iss_passes(
        self, key_pair, public_key_pem_file, mcp_deps_env
    ):
        """正确的 iss → 鉴权通过，返回用户身份。"""
        from app.api.deps import get_mcp_client

        private_key, _ = key_pair
        token = create_jwt(
            {
                "sub": "frank",
                "scope": "read",
                "iss": "http://localhost:3000",
                "aud": "bs",
            },
            private_key,
        )

        result = await get_mcp_client()(
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )
        assert result["username"] == "frank"
        assert result["mcp"] is True

    @pytest.mark.asyncio
    async def test_wrong_iss_raises_401(
        self, key_pair, public_key_pem_file, mcp_deps_env
    ):
        """错误的 iss → 401。"""
        from app.api.deps import get_mcp_client

        private_key, _ = key_pair
        token = create_jwt(
            {
                "sub": "grace",
                "scope": "read",
                "iss": "http://attacker.example.com",
                "aud": "bs",
            },
            private_key,
        )

        with pytest.raises(HTTPException) as ei:
            await get_mcp_client()(
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=token
                )
            )
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_iss_raises_401(
        self, key_pair, public_key_pem_file, mcp_deps_env
    ):
        """缺失 iss claim → 401。"""
        from app.api.deps import get_mcp_client

        private_key, _ = key_pair
        # 不传 iss claim
        token = create_jwt(
            {"sub": "heidi", "scope": "read", "aud": "bs"},
            private_key,
        )

        with pytest.raises(HTTPException) as ei:
            await get_mcp_client()(
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=token
                )
            )
        assert ei.value.status_code == 401
