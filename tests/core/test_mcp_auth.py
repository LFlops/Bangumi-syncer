"""MCP JWT 验签模块测试。"""

import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.mcp_auth import (
    PublicKeyNotFoundError,
    load_public_key,
    reload_public_key,
    verify_jwt,
)

# ---------------------------------------------------------------------------
# Fixtures: RSA key pairs
# ---------------------------------------------------------------------------


def _generate_key_pair():
    """生成 RSA 密钥对，返回 (private_key, public_key)。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _public_key_pem(public_key) -> bytes:
    """将公钥序列化为 PEM 格式。"""
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _private_key_pem(private_key) -> bytes:
    """将私钥序列化为 PEM 格式。"""
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def key_pair():
    """有效的 RSA 密钥对。"""
    return _generate_key_pair()


@pytest.fixture
def other_key_pair():
    """另一组 RSA 密钥对（用于签名不匹配测试）。"""
    return _generate_key_pair()


@pytest.fixture
def public_key_pem_file(tmp_path, key_pair) -> Path:
    """将公钥写入临时 PEM 文件并返回路径。"""
    _, public_key = key_pair
    pem_path = tmp_path / "mcp_public_key.pem"
    pem_path.write_bytes(_public_key_pem(public_key))
    return pem_path


def _base64url_encode(data: bytes) -> str:
    """Base64URL 编码（无填充）。"""
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(s: str) -> bytes:
    """Base64URL 解码（无填充）。"""
    import base64

    padding_needed = 4 - len(s) % 4
    if padding_needed != 4:
        s += "=" * padding_needed
    return base64.urlsafe_b64decode(s)


def _sign(data: bytes, private_key) -> bytes:
    """RSA-SHA256 签名。"""
    return private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def create_jwt(claims: dict, private_key, expires_in: int = 3600) -> str:
    """用 RS256 私钥签发 JWT（测试辅助）。"""
    import json

    header = {"alg": "RS256", "typ": "JWT"}
    now = int(time.time())
    claims_full = {
        **claims,
        "iat": now,
        "exp": now + expires_in,
    }
    header_b64 = _base64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _base64url_encode(
        json.dumps(claims_full, separators=(",", ":")).encode()
    )
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = _sign(signing_input, private_key)
    signature_b64 = _base64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{signature_b64}"


# ---------------------------------------------------------------------------
# 场景 1: 有效 JWT
# ---------------------------------------------------------------------------


class TestValidJwt:
    """有效 JWT 验签通过，解析出 sub/scope/exp 正确。"""

    def test_verify_valid_jwt_returns_claims(self, key_pair, public_key_pem_file):
        """有效 JWT 验签通过，返回解析后的 claims。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read write", "iss": "mcp", "aud": "bs"},
            private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is not None
        assert claims["sub"] == "user1"
        assert claims["scope"] == "read write"

    def test_verify_valid_jwt_returns_exp(self, key_pair, public_key_pem_file):
        """有效 JWT 验签通过，exp 存在且合理。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp", "aud": "bs"},
            private_key,
            expires_in=3600,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is not None
        assert "exp" in claims
        assert claims["exp"] > int(time.time())


# ---------------------------------------------------------------------------
# 场景 2: JWT 过期
# ---------------------------------------------------------------------------


class TestExpiredJwt:
    """JWT 过期时验签失败。"""

    def test_verify_expired_jwt_returns_none(self, key_pair, public_key_pem_file):
        """已过期的 JWT 应返回 None。"""
        private_key, _ = key_pair
        # 创建已过期 1 小时的 JWT
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp", "aud": "bs"},
            private_key,
            expires_in=-3600,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is None


# ---------------------------------------------------------------------------
# 场景 3: 签名无效 / 公钥不匹配
# ---------------------------------------------------------------------------


class TestInvalidSignature:
    """由其他密钥签名的 JWT → 验签失败。"""

    def test_verify_jwt_with_wrong_key_returns_none(
        self, key_pair, other_key_pair, public_key_pem_file
    ):
        """由不同私钥签名的 JWT 应返回 None。"""
        # 用 other_key_pair 的私钥签名，但用 key_pair 的公钥验签
        _, _ = key_pair
        other_private_key, _ = other_key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp", "aud": "bs"},
            other_private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is None

    def test_verify_tampered_jwt_returns_none(self, key_pair, public_key_pem_file):
        """被篡改的 JWT 应返回 None。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp", "aud": "bs"},
            private_key,
        )
        # 篡改 payload
        parts = jwt_token.split(".")
        tampered_payload = _base64url_encode(b'{"sub":"admin","scope":"admin"}')
        tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"

        claims = verify_jwt(
            tampered_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is None


# ---------------------------------------------------------------------------
# 场景 4: 公钥加载
# ---------------------------------------------------------------------------


class TestPublicKeyLoading:
    """从 PEM 文件加载 RSA 公钥并缓存；文件缺失时给出明确错误。"""

    def test_load_public_key_from_pem_file(self, key_pair, public_key_pem_file):
        """从 PEM 文件成功加载公钥。"""
        _, public_key = key_pair
        loaded = load_public_key(str(public_key_pem_file))
        assert loaded is not None
        # 序列化后应一致
        assert _public_key_pem(loaded) == _public_key_pem(public_key)

    def test_load_public_key_missing_file_raises_error(self):
        """公钥文件缺失时抛出 PublicKeyNotFoundError。"""
        with pytest.raises(PublicKeyNotFoundError):
            load_public_key("/nonexistent/path/mcp_public_key.pem")

    def test_load_public_key_caches_result(self, public_key_pem_file, monkeypatch):
        """公钥加载后应被缓存（第二次加载不重新读取文件）。"""
        # 第一次加载
        key1 = load_public_key(str(public_key_pem_file))
        # 记录文件读取次数
        read_count = 0
        original_read_bytes = Path.read_bytes

        def counting_read_bytes(self):
            nonlocal read_count
            read_count += 1
            return original_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
        # 第二次加载应使用缓存
        key2 = load_public_key(str(public_key_pem_file))
        assert key2 is key1
        assert read_count == 0


# ---------------------------------------------------------------------------
# 场景 5: scope 解析
# ---------------------------------------------------------------------------


class TestScopeParsing:
    """正确提取 scope claim。"""

    def test_scope_default_read_write(self, key_pair, public_key_pem_file):
        """scope 默认包含 read 和 write。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read write", "iss": "mcp", "aud": "bs"},
            private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is not None
        assert "read" in claims["scope"]
        assert "write" in claims["scope"]

    def test_scope_custom_value(self, key_pair, public_key_pem_file):
        """自定义 scope 值正确解析。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp", "aud": "bs"},
            private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is not None
        assert claims["scope"] == "read"


# ---------------------------------------------------------------------------
# 场景 6: 公钥重载
# ---------------------------------------------------------------------------


class TestPublicKeyReload:
    """文件变更后重新加载（或提供刷新方法）。"""

    def test_reload_public_key_after_file_change(
        self, key_pair, other_key_pair, public_key_pem_file
    ):
        """公钥文件变更后，reload_public_key 应重新加载。"""
        # 先加载原始公钥
        key1 = load_public_key(str(public_key_pem_file))

        # 写入新的公钥
        _, new_public_key = other_key_pair
        public_key_pem_file.write_bytes(_public_key_pem(new_public_key))

        # 重新加载
        key2 = reload_public_key(str(public_key_pem_file))
        assert key2 is not None
        # 新公钥与旧公钥不同
        assert _public_key_pem(key2) != _public_key_pem(key1)

    def test_reload_public_key_uses_new_key_for_verification(
        self, key_pair, other_key_pair, public_key_pem_file
    ):
        """重载后，新公钥能验签新 JWT，旧公钥不能。"""
        private_key, _ = key_pair
        other_private_key, other_public_key = other_key_pair

        # 先用旧公钥验签一个 JWT
        jwt_token_old = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp", "aud": "bs"},
            private_key,
        )
        claims = verify_jwt(
            jwt_token_old,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )
        assert claims is not None

        # 替换公钥文件
        public_key_pem_file.write_bytes(_public_key_pem(other_public_key))
        reload_public_key(str(public_key_pem_file))

        # 旧 JWT 不能再验签通过（公钥已变）
        claims = verify_jwt(
            jwt_token_old,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )
        assert claims is None

        # 新私钥签发的 JWT 可以验签通过
        jwt_token_new = create_jwt(
            {"sub": "user2", "scope": "write", "iss": "mcp", "aud": "bs"},
            other_private_key,
        )
        claims = verify_jwt(
            jwt_token_new,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )
        assert claims is not None
        assert claims["sub"] == "user2"


# ---------------------------------------------------------------------------
# 额外场景: audience 校验
# ---------------------------------------------------------------------------


class TestAudienceValidation:
    """audience 校验。"""

    def test_verify_jwt_with_wrong_audience_returns_none(
        self, key_pair, public_key_pem_file
    ):
        """audience 不匹配时返回 None。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp", "aud": "other"},
            private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is None

    def test_verify_jwt_with_correct_audience_returns_claims(
        self, key_pair, public_key_pem_file
    ):
        """audience 匹配时返回 claims。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp", "aud": "bs"},
            private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is not None


# ---------------------------------------------------------------------------
# 额外场景: 异常输入处理
# ---------------------------------------------------------------------------


class TestInvalidInput:
    """异常输入处理。"""

    def test_verify_malformed_jwt_returns_none(self, public_key_pem_file):
        """格式错误的 JWT 返回 None。"""
        claims = verify_jwt(
            "not.a.valid.jwt.structure",
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )
        assert claims is None

    def test_verify_empty_jwt_returns_none(self, public_key_pem_file):
        """空字符串 JWT 返回 None。"""
        claims = verify_jwt(
            "",
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )
        assert claims is None

    def test_verify_none_jwt_returns_none(self, public_key_pem_file):
        """None JWT 返回 None。"""
        claims = verify_jwt(
            None,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )
        assert claims is None


# ---------------------------------------------------------------------------
# 场景 7: aud 必须存在（M2）
# ---------------------------------------------------------------------------


class TestAudienceRequired:
    """aud claim 必须存在且等于期望 audience。"""

    def test_verify_jwt_without_aud_claims_returns_none(
        self, key_pair, public_key_pem_file
    ):
        """JWT 不含 aud claim → 验签失败（aud 必须存在）。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp"},
            private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
        )

        assert claims is None


# ---------------------------------------------------------------------------
# 场景 8: iss 校验（M3）
# ---------------------------------------------------------------------------


class TestIssuerValidation:
    """iss claim 必须存在且匹配期望 issuer。"""

    def test_verify_jwt_with_correct_iss_returns_claims(
        self, key_pair, public_key_pem_file
    ):
        """正确的 iss → 验签通过。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "mcp", "aud": "bs"},
            private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
            issuer="mcp",
        )

        assert claims is not None
        assert claims["sub"] == "user1"

    def test_verify_jwt_with_wrong_iss_returns_none(
        self, key_pair, public_key_pem_file
    ):
        """错误的 iss → 验签失败。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "iss": "attacker", "aud": "bs"},
            private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
            issuer="mcp",
        )

        assert claims is None

    def test_verify_jwt_with_missing_iss_returns_none(
        self, key_pair, public_key_pem_file
    ):
        """缺失 iss claim → 验签失败。"""
        private_key, _ = key_pair
        jwt_token = create_jwt(
            {"sub": "user1", "scope": "read", "aud": "bs"},
            private_key,
        )

        claims = verify_jwt(
            jwt_token,
            public_key_path=str(public_key_pem_file),
            audience="bs",
            issuer="mcp",
        )

        assert claims is None
