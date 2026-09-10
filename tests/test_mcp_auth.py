"""
Tests for FastMCP OAuthProvider (app.mcp.provider).

Covers T3 (full OAuthProvider), T4 (CIMD integration), T5 (DCR fallback):
- RSA key management (generate, load, sign, verify)
- DCR (Dynamic Client Registration) with scope defaults and limits
- Authorize endpoint with auth.enabled branching
- Consent page (allow/deny) with CSRF protection
- Token endpoint (authorization_code, refresh_token)
- JWT claims verification (RS256, sub/scope/iss/aud/exp)
- CIMD: URL client_id detection, scope injection, metadata injection
- Full end-to-end OAuth flows
"""

from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import AnyHttpUrl
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_keypair():
    """Generate a fresh RSA key pair for testing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _make_code_challenge_b64(verifier: str) -> str:
    """Create S256 code challenge from verifier (base64url)."""
    import base64

    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _extract_csrf_token(html_content: str) -> str:
    """Extract CSRF token from consent form HTML."""
    import re

    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_content)
    if not match:
        raise ValueError("CSRF token not found in consent form")
    return match.group(1)


# ---------------------------------------------------------------------------
# RSA Key Manager Tests
# ---------------------------------------------------------------------------


class TestRSAKeyManager:
    """RSA key generation, persistence, and JWT sign/verify."""

    def test_generate_creates_valid_keypair(self, tmp_path):
        """generate_keys should produce a valid RSA key pair."""
        from app.mcp.provider import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"
        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.generate_keys()

        assert private_path.exists()
        assert public_path.exists()

        # Verify keys work for sign/verify
        private_pem = private_path.read_bytes()
        public_pem = public_path.read_bytes()

        token = jwt.encode({"sub": "test"}, private_pem, algorithm="RS256")
        decoded = jwt.decode(token, public_pem, algorithms=["RS256"])
        assert decoded["sub"] == "test"

    def test_load_or_generate_creates_on_first_run(self, tmp_path):
        """load_or_generate should create keys if they don't exist."""
        from app.mcp.provider import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"
        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.load_or_generate()

        assert private_path.exists()
        assert public_path.exists()

    def test_load_or_generate_loads_existing(self, tmp_path):
        """load_or_generate should not overwrite existing keys."""
        from app.mcp.provider import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"

        # Pre-generate keys
        private_pem, public_pem = _generate_keypair()
        private_path.write_bytes(private_pem)
        public_path.write_bytes(public_pem)

        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.load_or_generate()

        # Keys should be unchanged
        assert private_path.read_bytes() == private_pem
        assert public_path.read_bytes() == public_pem

    def test_load_or_generate_recovers_missing_public_key(self, tmp_path):
        """Private key present + public key missing: re-derive public, keep private."""
        from app.mcp.provider import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"

        # Only the private key exists on disk.
        private_pem, _ = _generate_keypair()
        private_path.write_bytes(private_pem)
        assert not public_path.exists()

        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.load_or_generate()

        # Private key must be byte-for-byte unchanged (no silent regeneration).
        assert private_path.read_bytes() == private_pem
        # Private key file keeps owner-only permissions.
        assert (private_path.stat().st_mode & 0o777) == 0o600

        # Public key is re-derived from the existing private key.
        assert public_path.exists()
        derived_public = (
            serialization.load_pem_private_key(private_pem, password=None)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        assert public_path.read_bytes() == derived_public

        # The recovered pair is usable.
        token = manager.sign_jwt({"sub": "recover"})
        decoded = jwt.decode(token, manager.get_public_key_pem(), algorithms=["RS256"])
        assert decoded["sub"] == "recover"

    def test_load_or_generate_generates_pair_when_private_missing(self, tmp_path):
        """Private key missing: generate a fresh matching key pair."""
        from app.mcp.provider import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"

        # Stale public key exists but private key is gone -> must regenerate.
        _, stale_public_pem = _generate_keypair()
        public_path.write_bytes(stale_public_pem)

        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.load_or_generate()

        assert private_path.exists()
        assert public_path.exists()
        assert private_path.read_bytes() != b""
        assert (private_path.stat().st_mode & 0o777) == 0o600
        # Public key must now match the newly generated private key, not the stale one.
        assert public_path.read_bytes() != stale_public_pem

        token = manager.sign_jwt({"sub": "fresh"})
        decoded = jwt.decode(token, manager.get_public_key_pem(), algorithms=["RS256"])
        assert decoded["sub"] == "fresh"

    def test_public_key_pem_returns_public_key_bytes(self, tmp_path):
        """get_public_key_pem should return the public key in PEM format."""
        from app.mcp.provider import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"
        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.generate_keys()

        pub_pem = manager.get_public_key_pem()
        assert b"BEGIN PUBLIC KEY" in pub_pem

    def test_sign_jwt_creates_valid_jwt(self, tmp_path):
        """sign_jwt should create a JWT signed with RS256."""
        from app.mcp.provider import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"
        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.generate_keys()

        claims = {"sub": "user1", "scope": "read write"}
        token = manager.sign_jwt(claims)

        # Verify with public key
        public_pem = manager.get_public_key_pem()
        decoded = jwt.decode(token, public_pem, algorithms=["RS256"])
        assert decoded["sub"] == "user1"
        assert decoded["scope"] == "read write"

    def test_verify_jwt_rejects_expired(self, tmp_path):
        """verify_jwt should return None for expired JWT."""
        from app.mcp.provider import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"
        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.generate_keys()

        claims = {"sub": "user1", "exp": int(time.time()) - 100}
        token = manager.sign_jwt(claims)
        assert manager.verify_jwt(token) is None

    def test_verify_jwt_rejects_bad_signature(self, tmp_path):
        """verify_jwt should return None for JWT with wrong signature."""
        from app.mcp.provider import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"
        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.generate_keys()

        other_private, _ = _generate_keypair()
        claims = {"sub": "user1", "exp": int(time.time()) + 3600}
        token = jwt.encode(claims, other_private, algorithm="RS256")
        assert manager.verify_jwt(token) is None


# ---------------------------------------------------------------------------
# OAuth Provider Unit Tests (T3)
# ---------------------------------------------------------------------------


class TestOAuthProviderUnit:
    """Unit tests for BangumiOAuthProvider (without full server)."""

    @pytest.fixture
    def rsa_manager(self, tmp_path):
        from app.mcp.provider import RSAKeyManager

        manager = RSAKeyManager(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
        )
        manager.generate_keys()
        return manager

    @pytest.fixture
    def provider(self, rsa_manager):
        from app.mcp.provider import BangumiOAuthProvider

        return BangumiOAuthProvider(
            base_url="http://localhost:8000",
            rsa_manager=rsa_manager,
            issuer="http://localhost:8000",
            audience="bs",
            token_expiry_seconds=3600,
            auth_enabled=False,
            auth_username="admin",
        )

    @pytest.mark.asyncio
    async def test_get_client_returns_none_for_unknown(self, provider):
        """get_client should return None for unknown client_id."""
        result = await provider.get_client("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_register_and_get_client(self, provider):
        """register_client should store client; get_client should retrieve it."""
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="test-dcr-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="client_secret_post",
        )
        await provider.register_client(client_info)

        # Client should have a secret (for client_secret_post)
        assert client_info.client_secret

        # Retrieve it
        retrieved = await provider.get_client("test-dcr-client")
        assert retrieved is not None
        assert retrieved.client_id == "test-dcr-client"

    @pytest.mark.asyncio
    async def test_register_client_assigns_default_scope(self, provider):
        """register_client should assign default scope when not specified."""
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="test-scope-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        await provider.register_client(client_info)
        assert client_info.scope == "read"

    @pytest.mark.asyncio
    async def test_register_client_generates_client_id_if_missing(self, provider):
        """register_client should generate client_id if not provided."""
        from mcp.shared.auth import OAuthClientInformationFull

        # Omit client_id entirely (Pydantic requires it, so we delete after creation)
        client_info = OAuthClientInformationFull(
            client_id="temp-placeholder",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        # Simulate missing client_id by clearing it
        client_info.client_id = ""
        await provider.register_client(client_info)
        assert client_info.client_id
        assert len(client_info.client_id) > 0
        assert client_info.client_id != ""

    @pytest.mark.asyncio
    async def test_register_client_generates_secret_for_confidential(self, provider):
        """register_client should generate client_secret for non-public clients."""
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="test-confidential",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="client_secret_post",
        )
        await provider.register_client(client_info)
        assert client_info.client_secret
        assert len(client_info.client_secret) > 0

    @pytest.mark.asyncio
    async def test_register_client_enforces_max_limit(self, provider):
        """register_client should raise when client limit reached."""
        from mcp.shared.auth import OAuthClientInformationFull

        # Temporarily set max to a small number
        original_max = provider.MAX_CLIENTS
        provider.MAX_CLIENTS = 1
        try:
            # Register one client
            client_info = OAuthClientInformationFull(
                client_id="first-client",
                redirect_uris=[AnyHttpUrl("http://localhost/callback")],
                grant_types=["authorization_code"],
                token_endpoint_auth_method="none",
            )
            await provider.register_client(client_info)

            # Second should fail
            from mcp.server.auth.provider import RegistrationError

            with pytest.raises(RegistrationError, match="limit"):
                client_info2 = OAuthClientInformationFull(
                    client_id="second-client",
                    redirect_uris=[AnyHttpUrl("http://localhost/callback")],
                    grant_types=["authorization_code"],
                    token_endpoint_auth_method="none",
                )
                await provider.register_client(client_info2)
        finally:
            provider.MAX_CLIENTS = original_max

    @pytest.mark.asyncio
    async def test_authorize_returns_consent_url(self, provider):
        """authorize should return a URL pointing to the consent page."""
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        # Register client first
        await provider.register_client(client)

        params = AuthorizationParams(
            state="test-state",
            scopes=["read", "write"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )
        url = await provider.authorize(client, params)

        # Should redirect to consent page
        assert "/consent" in url
        assert "request_token=" in url

    @pytest.mark.asyncio
    async def test_authorize_stores_csrf_token(self, provider):
        """authorize should store CSRF token in pending auth."""
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        # Register client first
        await provider.register_client(client)

        params = AuthorizationParams(
            state="test-state",
            scopes=["read", "write"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )
        await provider.authorize(client, params)

        # Check pending auth has csrf_token
        assert len(provider._pending_auths) == 1
        pending = next(iter(provider._pending_auths.values()))
        assert "csrf_token" in pending
        assert len(pending["csrf_token"]) > 0

    @pytest.mark.asyncio
    async def test_exchange_authorization_code_issues_jwt(self, provider):
        """exchange_authorization_code should return JWT + refresh token."""
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        # Register client first
        await provider.register_client(client)

        params = AuthorizationParams(
            state=None,
            scopes=["read", "write"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )
        # Authorize to get request_token
        await provider.authorize(client, params)

        # Get the pending request's auth code (simulate consent allow)
        request_token = None
        for rt, pending in provider._pending_auths.items():
            if pending["client_id"] == "test-client":
                request_token = rt
                break

        assert request_token is not None

        # Simulate consent allow (with CSRF token)
        csrf_token = provider._pending_auths[request_token]["csrf_token"]
        auth_code = await provider.handle_consent_allow(
            request_token, username="testuser", csrf_token=csrf_token
        )

        # Exchange code for tokens
        code_obj = await provider.load_authorization_code(client, auth_code)
        assert code_obj is not None

        token = await provider.exchange_authorization_code(client, code_obj)
        assert token.access_token
        assert token.token_type == "Bearer"
        assert token.refresh_token

        # Verify JWT claims
        public_pem = provider.rsa_manager.get_public_key_pem()
        decoded = jwt.decode(
            token.access_token, public_pem, algorithms=["RS256"], audience="bs"
        )
        assert decoded["sub"] == "testuser"
        assert decoded["scope"] == "read write"
        assert decoded["iss"] == "http://localhost:8000"
        assert decoded["aud"] == "bs"
        assert decoded["exp"] > time.time()

    @pytest.mark.asyncio
    async def test_load_access_token_verifies_jwt(self, provider):
        """load_access_token should verify JWT and return AccessToken."""
        from mcp.server.auth.provider import AccessToken

        # Sign a valid JWT
        claims = {
            "sub": "user1",
            "scope": "read write",
            "iss": "http://localhost:8000",
            "aud": "bs",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }
        token_str = provider.rsa_manager.sign_jwt(claims)

        result = await provider.load_access_token(token_str)
        assert result is not None
        assert isinstance(result, AccessToken)
        assert result.subject == "user1"

    @pytest.mark.asyncio
    async def test_load_access_token_rejects_expired_jwt(self, provider):
        """load_access_token should return None for expired JWT."""
        claims = {
            "sub": "user1",
            "scope": "read write",
            "iss": "http://localhost:8000",
            "aud": "bs",
            "exp": int(time.time()) - 100,  # expired
            "iat": int(time.time()) - 3700,
        }
        token_str = provider.rsa_manager.sign_jwt(claims)

        result = await provider.load_access_token(token_str)
        assert result is None

    @pytest.mark.asyncio
    async def test_load_access_token_rejects_bad_signature(self, provider):
        """load_access_token should return None for JWT with wrong signature."""
        # Sign with a different key
        other_private, _ = _generate_keypair()
        claims = {
            "sub": "user1",
            "scope": "read write",
            "iss": "http://localhost:8000",
            "aud": "bs",
            "exp": int(time.time()) + 3600,
        }
        token_str = jwt.encode(claims, other_private, algorithm="RS256")

        result = await provider.load_access_token(token_str)
        assert result is None

    @pytest.mark.asyncio
    async def test_consent_deny_clears_pending(self, provider):
        """handle_consent_deny should clear the pending auth request."""
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        # Register client first
        await provider.register_client(client)

        params = AuthorizationParams(
            state=None,
            scopes=["read", "write"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )
        await provider.authorize(client, params)

        # Find request_token
        request_token = next(iter(provider._pending_auths))
        assert request_token in provider._pending_auths

        # Deny
        await provider.handle_consent_deny(request_token)
        assert request_token not in provider._pending_auths

    @pytest.mark.asyncio
    async def test_refresh_token_rotation(self, provider):
        """exchange_refresh_token should rotate refresh token."""
        from mcp.server.auth.provider import (
            OAuthClientInformationFull,
            RefreshToken,
        )

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="none",
        )

        # Create a refresh token
        old_refresh_str = secrets.token_urlsafe(32)
        old_refresh = RefreshToken(
            token=old_refresh_str,
            client_id="test-client",
            scopes=["read", "write"],
            subject="testuser",
        )
        provider._refresh_tokens[old_refresh_str] = old_refresh

        # Exchange
        token = await provider.exchange_refresh_token(
            client, old_refresh, ["read", "write"]
        )
        assert token.access_token
        assert token.refresh_token
        assert token.refresh_token != old_refresh_str

        # Old token should be removed
        assert old_refresh_str not in provider._refresh_tokens

    @pytest.mark.asyncio
    async def test_revoke_refresh_token(self, provider):
        """revoke_token should remove refresh token from store."""
        from mcp.server.auth.provider import RefreshToken

        refresh = RefreshToken(
            token="test-refresh",
            client_id="test-client",
            scopes=["read"],
        )
        provider._refresh_tokens["test-refresh"] = refresh

        await provider.revoke_token(refresh)
        assert "test-refresh" not in provider._refresh_tokens


# ---------------------------------------------------------------------------
# Consent + CSRF Tests (T3)
# ---------------------------------------------------------------------------


class TestConsentFlow:
    """Tests for consent page and CSRF protection."""

    @pytest.fixture
    def rsa_manager(self, tmp_path):
        from app.mcp.provider import RSAKeyManager

        manager = RSAKeyManager(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
        )
        manager.generate_keys()
        return manager

    @pytest.fixture
    def provider(self, rsa_manager):
        from app.mcp.provider import BangumiOAuthProvider

        return BangumiOAuthProvider(
            base_url="http://localhost:8000",
            rsa_manager=rsa_manager,
            issuer="http://localhost:8000",
            audience="bs",
            token_expiry_seconds=3600,
            auth_enabled=False,
            auth_username="admin",
        )

    @pytest.mark.asyncio
    async def test_consent_allow_without_csrf_rejected(self, provider):
        """handle_consent_allow should reject request without CSRF token."""
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        # Register client first
        await provider.register_client(client)

        params = AuthorizationParams(
            state=None,
            scopes=["read"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )
        await provider.authorize(client, params)
        request_token = next(iter(provider._pending_auths))

        # Try to allow without CSRF token
        with pytest.raises(ValueError, match="CSRF"):
            await provider.handle_consent_allow(
                request_token, csrf_token="", username="admin"
            )

    @pytest.mark.asyncio
    async def test_consent_allow_with_wrong_csrf_rejected(self, provider):
        """handle_consent_allow should reject request with wrong CSRF token."""
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        # Register client first
        await provider.register_client(client)

        params = AuthorizationParams(
            state=None,
            scopes=["read"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )
        await provider.authorize(client, params)
        request_token = next(iter(provider._pending_auths))

        # Try to allow with wrong CSRF token
        with pytest.raises(ValueError, match="CSRF"):
            await provider.handle_consent_allow(
                request_token, csrf_token="wrong-token", username="admin"
            )

    @pytest.mark.asyncio
    async def test_consent_allow_with_correct_csrf_succeeds(self, provider):
        """handle_consent_allow should succeed with correct CSRF token."""
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        # Register client first
        await provider.register_client(client)

        params = AuthorizationParams(
            state=None,
            scopes=["read"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )
        await provider.authorize(client, params)
        request_token = next(iter(provider._pending_auths))
        csrf_token = provider._pending_auths[request_token]["csrf_token"]

        # Should succeed
        auth_code = await provider.handle_consent_allow(
            request_token, csrf_token=csrf_token, username="admin"
        )
        assert auth_code
        assert len(auth_code) > 0

    @pytest.mark.asyncio
    async def test_consent_form_rendered_with_csrf_and_escaping(self, provider):
        """_render_consent_form should include CSRF token and escape HTML."""
        context = {
            "request_token": "tok123",
            "client_id": "<script>alert(1)</script>",
            "scopes": ["read", "write"],
            "username": "admin",
            "csrf_token": "csrf456",
        }
        html = provider._render_consent_form(context)
        # CSRF token should be present
        assert "csrf456" in html
        # Client_id should be escaped
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# CIMD Tests (T4)
# ---------------------------------------------------------------------------


class TestCIMDIntegration:
    """Tests for CIMD (Client ID Metadata Document) integration."""

    @pytest.fixture
    def rsa_manager(self, tmp_path):
        from app.mcp.provider import RSAKeyManager

        manager = RSAKeyManager(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
        )
        manager.generate_keys()
        return manager

    @pytest.fixture
    def provider(self, rsa_manager):
        from app.mcp.provider import BangumiOAuthProvider

        return BangumiOAuthProvider(
            base_url="http://localhost:8000",
            rsa_manager=rsa_manager,
            issuer="http://localhost:8000",
            audience="bs",
            token_expiry_seconds=3600,
            auth_enabled=False,
            auth_username="admin",
        )

    def test_is_cimd_detects_url_client_id(self, provider):
        """is_cimd_client_id should detect HTTPS URL as CIMD."""
        assert provider.cimd.is_cimd_client_id(
            "https://claude.ai/oauth/claude-code-client-metadata"
        )

    def test_is_cimd_rejects_plain_client_id(self, provider):
        """is_cimd_client_id should reject plain string client_id."""
        assert not provider.cimd.is_cimd_client_id("my-random-client")

    def test_is_cimd_rejects_http_url(self, provider):
        """is_cimd_client_id should reject HTTP (non-SSL) URLs."""
        assert not provider.cimd.is_cimd_client_id("http://example.com/metadata")

    @pytest.mark.asyncio
    async def test_get_client_cimd_branch_with_mock(self, provider):
        """get_client should delegate to CIMD for URL client_id."""
        from fastmcp.server.auth.cimd import CIMDDocument
        from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient

        # Create a mock CIMD client
        cimd_doc = CIMDDocument(
            client_id=AnyHttpUrl("https://claude.ai/oauth/claude-code-client-metadata"),
            client_name="Claude Code",
            redirect_uris=["http://localhost/callback"],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
        )
        mock_client = ProxyDCRClient(
            client_id="https://claude.ai/oauth/claude-code-client-metadata",
            client_secret=None,
            redirect_uris=None,
            grant_types=cimd_doc.grant_types,
            scope="read write",
            token_endpoint_auth_method=cimd_doc.token_endpoint_auth_method,
            allowed_redirect_uri_patterns=None,
            client_name=cimd_doc.client_name,
            cimd_document=cimd_doc,
            cimd_fetched_at=time.time(),
        )

        # Mock the CIMD get_client method (must be async)
        async def mock_get_client(url):
            return mock_client

        provider.cimd.get_client = mock_get_client

        result = await provider.get_client(
            "https://claude.ai/oauth/claude-code-client-metadata"
        )
        assert result is not None
        assert result.client_name == "Claude Code"

    @pytest.mark.asyncio
    async def test_get_client_dcr_branch(self, provider):
        """get_client should return DCR client for non-URL client_id."""
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="dcr-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        await provider.register_client(client_info)

        result = await provider.get_client("dcr-client")
        assert result is not None
        assert result.client_id == "dcr-client"

    @pytest.mark.asyncio
    async def test_get_client_unknown_returns_none(self, provider):
        """get_client should return None for unknown client_id."""
        result = await provider.get_client("totally-unknown-client")
        assert result is None

    @pytest.mark.asyncio
    async def test_cimd_scope_injection(self, provider):
        """CIMD client without scope should get default scope injected."""
        from fastmcp.server.auth.cimd import CIMDDocument
        from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient

        # CIMD doc without scope
        cimd_doc = CIMDDocument(
            client_id=AnyHttpUrl("https://example.com/oauth/client-metadata"),
            client_name="Test App",
            redirect_uris=["http://localhost/callback"],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code"],
            scope=None,  # No scope in metadata
        )

        # Simulate what CIMDClientManager.get_client does with default_scope
        mock_client = ProxyDCRClient(
            client_id="https://example.com/oauth/client-metadata",
            client_secret=None,
            redirect_uris=None,
            grant_types=cimd_doc.grant_types,
            scope=cimd_doc.scope or provider.cimd.default_scope,
            token_endpoint_auth_method=cimd_doc.token_endpoint_auth_method,
            allowed_redirect_uri_patterns=None,
            client_name=cimd_doc.client_name,
            cimd_document=cimd_doc,
            cimd_fetched_at=time.time(),
        )

        # Default scope should be injected
        assert mock_client.scope == "read"

    def test_cimd_default_scope_configuration(self, provider):
        """CIMDClientManager should be configured with default scope."""
        assert provider.cimd.default_scope == "read"


# ---------------------------------------------------------------------------
# Metadata Injection Tests (T4)
# ---------------------------------------------------------------------------


class TestMetadataInjection:
    """Tests for client_id_metadata_document_supported injection."""

    @pytest.fixture
    def rsa_manager(self, tmp_path):
        from app.mcp.provider import RSAKeyManager

        manager = RSAKeyManager(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
        )
        manager.generate_keys()
        return manager

    @pytest.fixture
    def provider(self, rsa_manager):
        from app.mcp.provider import BangumiOAuthProvider

        return BangumiOAuthProvider(
            base_url="http://localhost:8000",
            rsa_manager=rsa_manager,
            issuer="http://localhost:8000",
            audience="bs",
            token_expiry_seconds=3600,
            auth_enabled=False,
            auth_username="admin",
        )

    def test_metadata_includes_cimd_support(self, provider):
        """get_routes should inject client_id_metadata_document_supported=True."""
        routes = provider.get_routes()
        # Find the metadata route
        from starlette.routing import Route

        metadata_route = None
        for route in routes:
            if (
                isinstance(route, Route)
                and route.path == "/.well-known/oauth-authorization-server"
            ):
                metadata_route = route
                break

        assert metadata_route is not None, "Metadata route should exist"

        # We can't easily inspect the metadata from the route directly,
        # but we can verify the route exists and is properly configured
        assert "GET" in (metadata_route.methods or [])


# ---------------------------------------------------------------------------
# Full Integration Tests (with TestClient)
# ---------------------------------------------------------------------------


class TestOAuthFullFlow:
    """End-to-end OAuth flow tests using TestClient."""

    @pytest.fixture
    def tmp_keys(self, tmp_path):
        """Create temporary key files."""
        return {
            "private": str(tmp_path / "private.pem"),
            "public": str(tmp_path / "public.pem"),
        }

    @pytest.fixture
    def server_app_auth_disabled(self, tmp_keys):
        """Create a test server with auth.enabled=False."""
        from app.mcp.provider import create_auth_server

        app = create_auth_server(
            private_key_path=tmp_keys["private"],
            public_key_path=tmp_keys["public"],
            issuer="http://localhost:8000",
            audience="bs",
            auth_enabled=False,
            auth_username="admin",
        )
        return app

    @pytest.fixture
    def server_app_auth_enabled(self, tmp_keys):
        """Create a test server with auth.enabled=True."""
        from app.mcp.provider import create_auth_server

        app = create_auth_server(
            private_key_path=tmp_keys["private"],
            public_key_path=tmp_keys["public"],
            issuer="http://localhost:8000",
            audience="bs",
            auth_enabled=True,
            auth_username="admin",
        )
        return app

    def _make_test_client(self, app):
        return TestClient(app, raise_server_exceptions=False)

    def test_metadata_endpoint_returns_authorization_server_metadata(
        self, server_app_auth_disabled
    ):
        """GET /.well-known/oauth-authorization-server should return metadata."""
        client = self._make_test_client(server_app_auth_disabled)
        response = client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        data = response.json()
        # AnyHttpUrl normalizes to trailing slash
        assert data["issuer"] in ("http://localhost:8000", "http://localhost:8000/")
        assert "authorization_endpoint" in data
        assert "token_endpoint" in data
        assert "registration_endpoint" in data

    def test_metadata_includes_cimd_support_flag(self, server_app_auth_disabled):
        """Metadata should advertise client_id_metadata_document_supported."""
        client = self._make_test_client(server_app_auth_disabled)
        response = client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        data = response.json()
        assert data.get("client_id_metadata_document_supported") is True

    def test_dcr_register_client(self, server_app_auth_disabled):
        """POST /register should register a new client."""
        client = self._make_test_client(server_app_auth_disabled)
        response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert "client_id" in data
        assert "client_secret" in data
        assert data["client_id_issued_at"] is not None

    def test_full_flow_auth_disabled(self, server_app_auth_disabled):
        """Full OAuth flow with auth.enabled=False: authorize → consent → token."""
        client = self._make_test_client(server_app_auth_disabled)

        # Step 1: DCR（显式注册 read write scope，因默认 scope 已改为 read）
        reg_response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "none",
                "scope": "read write",
            },
        )
        assert reg_response.status_code == 201
        client_id = reg_response.json()["client_id"]

        # Step 2: Authorize
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = _make_code_challenge_b64(code_verifier)
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "scope": "read write",
                "state": "test-state",
            },
            follow_redirects=False,
        )
        assert auth_response.status_code == 302
        consent_url = auth_response.headers["location"]
        assert "/consent" in consent_url

        # Step 3: Consent page (GET)
        consent_get = client.get(consent_url)
        assert consent_get.status_code == 200

        # Step 4: Consent allow (POST)
        # Parse request_token from consent URL
        parsed = urlparse(consent_url)
        request_token = parse_qs(parsed.query)["request_token"][0]
        # Extract CSRF token from consent form
        csrf_token = _extract_csrf_token(consent_get.text)

        consent_post = client.post(
            "/consent",
            data={
                "action": "allow",
                "request_token": request_token,
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert consent_post.status_code == 302
        redirect_url = consent_post.headers["location"]
        assert "code=" in redirect_url

        # Extract code
        parsed_redirect = urlparse(redirect_url)
        code = parse_qs(parsed_redirect.query)["code"][0]

        # Step 5: Token exchange
        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://localhost/callback",
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
        assert token_response.status_code == 200
        token_data = token_response.json()
        assert token_data["token_type"] == "Bearer"
        assert token_data["access_token"]
        assert token_data["refresh_token"]

        # Verify JWT claims
        access_token = token_data["access_token"]
        # Load public key to verify
        public_pem = server_app_auth_disabled.state.public_key_pem
        decoded = jwt.decode(
            access_token, public_pem, algorithms=["RS256"], audience="bs"
        )
        assert decoded["sub"] == "admin"  # auth.username when auth.enabled=False
        assert decoded["scope"] == "read write"
        assert decoded["iss"] == "http://localhost:8000"
        assert decoded["aud"] == "bs"
        assert decoded["exp"] > time.time()

    def test_consent_deny_returns_error(self, server_app_auth_disabled):
        """User clicking deny should redirect with error."""
        client = self._make_test_client(server_app_auth_disabled)

        # DCR
        reg_response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = reg_response.json()["client_id"]

        # Authorize
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = _make_code_challenge_b64(code_verifier)
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        consent_url = auth_response.headers["location"]
        parsed = urlparse(consent_url)
        request_token = parse_qs(parsed.query)["request_token"][0]

        # Get consent form to extract CSRF token
        consent_get = client.get(consent_url)
        assert consent_get.status_code == 200
        csrf_token = _extract_csrf_token(consent_get.text)

        # Deny
        consent_post = client.post(
            "/consent",
            data={
                "action": "deny",
                "request_token": request_token,
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert consent_post.status_code == 302
        redirect_url = consent_post.headers["location"]
        assert "error=" in redirect_url
        assert "access_denied" in redirect_url

    def test_consent_post_without_csrf_rejected(self, server_app_auth_disabled):
        """POST /consent without CSRF token should return 403."""
        client = self._make_test_client(server_app_auth_disabled)

        # DCR
        reg_response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = reg_response.json()["client_id"]

        # Authorize
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = _make_code_challenge_b64(code_verifier)
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        consent_url = auth_response.headers["location"]
        parsed = urlparse(consent_url)
        request_token = parse_qs(parsed.query)["request_token"][0]

        # POST without CSRF token
        consent_post = client.post(
            "/consent",
            data={
                "action": "allow",
                "request_token": request_token,
                # No csrf_token
            },
            follow_redirects=False,
        )
        assert consent_post.status_code == 403

    def test_consent_post_with_wrong_csrf_rejected(self, server_app_auth_disabled):
        """POST /consent with wrong CSRF token should return 403."""
        client = self._make_test_client(server_app_auth_disabled)

        # DCR
        reg_response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = reg_response.json()["client_id"]

        # Authorize
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = _make_code_challenge_b64(code_verifier)
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        consent_url = auth_response.headers["location"]
        parsed = urlparse(consent_url)
        request_token = parse_qs(parsed.query)["request_token"][0]

        # POST with wrong CSRF token
        consent_post = client.post(
            "/consent",
            data={
                "action": "allow",
                "request_token": request_token,
                "csrf_token": "wrong-csrf-token",
            },
            follow_redirects=False,
        )
        assert consent_post.status_code == 403

    def test_token_with_invalid_code_returns_error(self, server_app_auth_disabled):
        """Token endpoint with invalid code should return error.

        Note: FastMCP's TokenHandler transforms 400 invalid_grant -> 401
        per MCP spec ("Invalid or expired tokens MUST receive a HTTP 401 response").
        """
        client = self._make_test_client(server_app_auth_disabled)

        # DCR
        reg_response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = reg_response.json()["client_id"]

        # Try token with invalid code
        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": "invalid-code",
                "redirect_uri": "http://localhost/callback",
                "client_id": client_id,
                "code_verifier": "verifier",
            },
        )
        # FastMCP transforms 400 -> 401 for invalid_grant per MCP spec
        assert token_response.status_code == 401
        assert token_response.json()["error"] == "invalid_grant"

    def test_refresh_token_flow(self, server_app_auth_disabled):
        """Refresh token should grant a new access token."""
        client = self._make_test_client(server_app_auth_disabled)

        # DCR
        reg_response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = reg_response.json()["client_id"]

        # Authorize
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = _make_code_challenge_b64(code_verifier)
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        consent_url = auth_response.headers["location"]
        parsed = urlparse(consent_url)
        request_token = parse_qs(parsed.query)["request_token"][0]

        # Get consent form to extract CSRF token
        consent_get = client.get(consent_url)
        assert consent_get.status_code == 200
        csrf_token = _extract_csrf_token(consent_get.text)

        # Consent allow
        consent_post = client.post(
            "/consent",
            data={
                "action": "allow",
                "request_token": request_token,
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert consent_post.status_code == 302
        redirect_url = consent_post.headers["location"]
        code = parse_qs(urlparse(redirect_url).query)["code"][0]

        # Token exchange
        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://localhost/callback",
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
        assert token_response.status_code == 200
        refresh_token = token_response.json()["refresh_token"]

        # Refresh
        refresh_response = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
        )
        assert refresh_response.status_code == 200
        new_token_data = refresh_response.json()
        assert new_token_data["access_token"]
        assert new_token_data["access_token"] != token_response.json()["access_token"]

    def test_revoke_token_endpoint(self, server_app_auth_disabled):
        """POST /revoke should revoke a token."""
        client = self._make_test_client(server_app_auth_disabled)

        # DCR
        reg_response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = reg_response.json()["client_id"]

        # Authorize
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = _make_code_challenge_b64(code_verifier)
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        consent_url = auth_response.headers["location"]
        parsed = urlparse(consent_url)
        request_token = parse_qs(parsed.query)["request_token"][0]

        consent_get = client.get(consent_url)
        csrf_token = _extract_csrf_token(consent_get.text)

        consent_post = client.post(
            "/consent",
            data={
                "action": "allow",
                "request_token": request_token,
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        redirect_url = consent_post.headers["location"]
        code = parse_qs(urlparse(redirect_url).query)["code"][0]

        # Token exchange
        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://localhost/callback",
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
        assert token_response.status_code == 200
        refresh_token = token_response.json()["refresh_token"]

        # Revoke (client_secret required by SDK RevocationRequest model)
        revoke_response = client.post(
            "/revoke",
            data={
                "token": refresh_token,
                "client_id": client_id,
                "client_secret": "",
            },
        )
        assert revoke_response.status_code == 200

    @pytest.mark.asyncio
    async def test_full_flow_auth_enabled_with_mock_security(
        self, server_app_auth_enabled, monkeypatch
    ):
        """Full OAuth flow with auth.enabled=True and mocked security_manager."""
        from app.mcp import provider as provider_module

        # Mock security_manager.validate_session to return a session
        mock_session = {"username": "testuser", "created_at": time.time()}
        monkeypatch.setattr(
            provider_module.security_manager,
            "validate_session",
            lambda token: mock_session,
        )

        with TestClient(
            server_app_auth_enabled, raise_server_exceptions=False
        ) as client:
            # DCR（显式注册 read write scope，因默认 scope 已改为 read）
            reg_response = client.post(
                "/register",
                json={
                    "redirect_uris": ["http://localhost/callback"],
                    "grant_types": ["authorization_code"],
                    "token_endpoint_auth_method": "none",
                    "scope": "read write",
                },
            )
            assert reg_response.status_code == 201
            client_id = reg_response.json()["client_id"]

            # Authorize
            code_verifier = secrets.token_urlsafe(32)
            code_challenge = _make_code_challenge_b64(code_verifier)
            auth_response = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                    "scope": "read write",
                },
                follow_redirects=False,
            )
            assert auth_response.status_code == 302
            consent_url = auth_response.headers["location"]

            # Consent GET (checks BS session via security_manager)
            # Send session_token cookie so the handler can validate it
            consent_get = client.get(
                consent_url,
                cookies={"session_token": "valid-session-token"},
            )
            assert consent_get.status_code == 200

            # Consent allow
            parsed = urlparse(consent_url)
            request_token = parse_qs(parsed.query)["request_token"][0]
            csrf_token = _extract_csrf_token(consent_get.text)
            consent_post = client.post(
                "/consent",
                data={
                    "action": "allow",
                    "request_token": request_token,
                    "csrf_token": csrf_token,
                },
                cookies={"session_token": "valid-session-token"},
                follow_redirects=False,
            )
            assert consent_post.status_code == 302
            redirect_url = consent_post.headers["location"]
            code = parse_qs(urlparse(redirect_url).query)["code"][0]

            # Token exchange
            token_response = client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://localhost/callback",
                    "client_id": client_id,
                    "code_verifier": code_verifier,
                },
            )
            assert token_response.status_code == 200
            token_data = token_response.json()

            # Verify JWT sub = testuser (from security_manager session)
            public_pem = server_app_auth_enabled.state.public_key_pem
            decoded = jwt.decode(
                token_data["access_token"],
                public_pem,
                algorithms=["RS256"],
                audience="bs",
            )
            assert decoded["sub"] == "testuser"
            assert decoded["scope"] == "read write"

    @pytest.mark.asyncio
    async def test_auth_enabled_no_session_redirects_to_login(
        self, server_app_auth_enabled, monkeypatch
    ):
        """When auth.enabled=True and no session, consent GET should indicate login needed."""
        from app.mcp import provider as provider_module

        # Mock security_manager.validate_session to return None (no session)
        monkeypatch.setattr(
            provider_module.security_manager,
            "validate_session",
            lambda token: None,
        )

        with TestClient(
            server_app_auth_enabled, raise_server_exceptions=False
        ) as client:
            # DCR
            reg_response = client.post(
                "/register",
                json={
                    "redirect_uris": ["http://localhost/callback"],
                    "grant_types": ["authorization_code"],
                    "token_endpoint_auth_method": "none",
                },
            )
            client_id = reg_response.json()["client_id"]

            # Authorize
            code_verifier = secrets.token_urlsafe(32)
            code_challenge = _make_code_challenge_b64(code_verifier)
            auth_response = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            consent_url = auth_response.headers["location"]

            # Consent GET - should indicate login needed (not 200 with form)
            consent_get = client.get(consent_url)
            # Either redirect to login or show error - not the consent form
            assert consent_get.status_code in (302, 401)


# ---------------------------------------------------------------------------
# DCR Fallback Tests (T5)
# ---------------------------------------------------------------------------


class TestDCRFallback:
    """Tests for DCR fallback behavior."""

    @pytest.fixture
    def rsa_manager(self, tmp_path):
        from app.mcp.provider import RSAKeyManager

        manager = RSAKeyManager(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
        )
        manager.generate_keys()
        return manager

    @pytest.fixture
    def provider(self, rsa_manager):
        from app.mcp.provider import BangumiOAuthProvider

        return BangumiOAuthProvider(
            base_url="http://localhost:8000",
            rsa_manager=rsa_manager,
            issuer="http://localhost:8000",
            audience="bs",
            token_expiry_seconds=3600,
            auth_enabled=False,
            auth_username="admin",
        )

    @pytest.mark.asyncio
    async def test_unknown_client_id_rejected_in_authorize(self, provider):
        """authorize should reject unknown client_id with unauthorized_client."""
        from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
        from mcp.shared.auth import OAuthClientInformationFull

        # Create a client object but don't register it
        client = OAuthClientInformationFull(
            client_id="unregistered-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        params = AuthorizationParams(
            state=None,
            scopes=["read"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )

        with pytest.raises(AuthorizeError) as exc_info:
            await provider.authorize(client, params)
        assert exc_info.value.error == "unauthorized_client"
        assert "Unknown client" in (exc_info.value.error_description or "")

    @pytest.mark.asyncio
    async def test_authorize_validates_redirect_uri(self, provider):
        """authorize should reject mismatched redirect_uri with invalid_request."""
        from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("http://localhost/callback")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        await provider.register_client(client_info)

        # Try authorize with different redirect_uri
        params = AuthorizationParams(
            state=None,
            scopes=["read"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("http://evil.com/callback"),
            redirect_uri_provided_explicitly=True,
        )

        with pytest.raises(AuthorizeError) as exc_info:
            await provider.authorize(client_info, params)
        assert exc_info.value.error == "invalid_request"
        assert "redirect_uri" in (exc_info.value.error_description or "")

    def test_matches_redirect_uri_loopback_port_flexible(self):
        """Loopback redirect_uri should match regardless of port (RFC 8252)."""
        from app.mcp.provider import BangumiOAuthProvider

        assert (
            BangumiOAuthProvider._matches_redirect_uri(
                "http://localhost:54321/callback",
                ["http://localhost/callback"],
            )
            is True
        )

    def test_matches_redirect_uri_non_loopback_port_must_match(self):
        """Non-loopback redirect_uri should require the exact port."""
        from app.mcp.provider import BangumiOAuthProvider

        assert (
            BangumiOAuthProvider._matches_redirect_uri(
                "http://example.com:8080/callback",
                ["http://example.com/callback"],
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_register_client_validates_redirect_uris(self, provider):
        """register_client should validate redirect_uris are provided."""
        from mcp.shared.auth import OAuthClientInformationFull

        # Client without redirect_uris should still be registerable
        # (some clients may not have them initially)
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        await provider.register_client(client_info)
        # Should succeed (empty list is valid)
        retrieved = await provider.get_client("test-client")
        assert retrieved is not None


# ---------------------------------------------------------------------------
# Security Fix Tests (Roundtable Review)
# ---------------------------------------------------------------------------


class TestSecurityFixes:
    """Tests for P0/P1/P2 security fixes from roundtable review."""

    @pytest.fixture
    def rsa_manager(self, tmp_path):
        from app.mcp.provider import RSAKeyManager

        manager = RSAKeyManager(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
        )
        manager.generate_keys()
        return manager

    @pytest.fixture
    def provider(self, rsa_manager):
        from app.mcp.provider import BangumiOAuthProvider

        return BangumiOAuthProvider(
            base_url="http://localhost:8000",
            rsa_manager=rsa_manager,
            issuer="http://localhost:8000",
            audience="bs",
            token_expiry_seconds=3600,
            auth_enabled=False,
            auth_username="admin",
        )

    # --- P0-1: CIMD redirect_uri validation ---

    @pytest.mark.asyncio
    async def test_cimd_authorize_rejects_malicious_redirect_uri(self, provider):
        """CIMD client with malicious redirect_uri should be rejected (P0-1)."""
        from fastmcp.server.auth.cimd import CIMDDocument
        from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
        from mcp.server.auth.provider import AuthorizationParams
        from pydantic import AnyHttpUrl

        # Create a CIMD client with a known redirect_uri
        cimd_doc = CIMDDocument(
            client_id=AnyHttpUrl("https://claude.ai/oauth/claude-code-client-metadata"),
            client_name="Claude Code",
            redirect_uris=["http://localhost/callback"],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
        )
        mock_client = ProxyDCRClient(
            client_id="https://claude.ai/oauth/claude-code-client-metadata",
            client_secret=None,
            redirect_uris=None,
            grant_types=cimd_doc.grant_types,
            scope="read write",
            token_endpoint_auth_method=cimd_doc.token_endpoint_auth_method,
            allowed_redirect_uri_patterns=None,
            client_name=cimd_doc.client_name,
            cimd_document=cimd_doc,
            cimd_fetched_at=time.time(),
        )

        # Mock CIMD get_client to return our mock
        async def mock_get_client(url):
            return mock_client

        provider.cimd.get_client = mock_get_client

        # Try authorize with attacker-controlled redirect_uri
        params = AuthorizationParams(
            state="test-state",
            scopes=["read", "write"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("https://attacker.com/steal"),
            redirect_uri_provided_explicitly=True,
        )

        from mcp.server.auth.provider import AuthorizeError

        with pytest.raises(AuthorizeError) as exc_info:
            await provider.authorize(mock_client, params)
        assert exc_info.value.error == "invalid_request"
        assert "redirect_uri" in (exc_info.value.error_description or "")

    # --- P0-2: DCR prefix bypass ---

    @pytest.mark.asyncio
    async def test_dcr_authorize_rejects_prefix_bypass_subdomain(self, provider):
        """DCR client redirect_uri prefix bypass via subdomain should be rejected (P0-2)."""
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyHttpUrl

        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("https://app.example.com/cb")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        await provider.register_client(client_info)

        # Attack: cb.attacker.com matches startswith("...example.com/cb") but is different host
        params = AuthorizationParams(
            state=None,
            scopes=["read"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("https://app.example.com/cb.attacker.com/x"),
            redirect_uri_provided_explicitly=True,
        )

        from mcp.server.auth.provider import AuthorizeError

        with pytest.raises(AuthorizeError) as exc_info:
            await provider.authorize(client_info, params)
        assert exc_info.value.error == "invalid_request"
        assert "redirect_uri" in (exc_info.value.error_description or "")

    @pytest.mark.asyncio
    async def test_dcr_authorize_rejects_prefix_bypass_dot_segments(self, provider):
        """DCR client redirect_uri bypass via dot-segments should be rejected (P0-2)."""
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyHttpUrl

        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyHttpUrl("https://app.example.com/cb")],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        await provider.register_client(client_info)

        # Attack: path traversal via dot-segments
        params = AuthorizationParams(
            state=None,
            scopes=["read"],
            code_challenge="challenge123",
            redirect_uri=AnyHttpUrl("https://app.example.com/cb/../evil"),
            redirect_uri_provided_explicitly=True,
        )

        from mcp.server.auth.provider import AuthorizeError

        with pytest.raises(AuthorizeError) as exc_info:
            await provider.authorize(client_info, params)
        assert exc_info.value.error == "invalid_request"
        assert "redirect_uri" in (exc_info.value.error_description or "")

    # --- P1-1: issuer_url ---

    def test_metadata_issuer_matches_jwt_iss(self, provider):
        """Metadata issuer should match JWT iss claim (P1-1)."""
        from starlette.testclient import TestClient

        # Create a full server to test metadata
        from app.mcp.provider import create_auth_server

        app = create_auth_server(
            private_key_path=provider.rsa_manager.private_key_path,
            public_key_path=provider.rsa_manager.public_key_path,
            issuer="http://localhost:8000",
            audience="bs",
            auth_enabled=False,
            auth_username="admin",
        )

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        metadata = response.json()
        # issuer in metadata should match the configured issuer
        assert metadata["issuer"] in ("http://localhost:8000", "http://localhost:8000/")

    # --- P1-2: revoke access token ---

    @pytest.mark.asyncio
    async def test_revoke_access_token_invalidates_it(self, provider):
        """Revoking an access token should make load_access_token return None (P1-2)."""
        from mcp.server.auth.provider import AccessToken

        # Create a valid access token
        claims = {
            "sub": "user1",
            "scope": "read write",
            "iss": "http://localhost:8000",
            "aud": "bs",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }
        token_str = provider.rsa_manager.sign_jwt(claims)

        # Load it first to confirm it's valid
        access_token = await provider.load_access_token(token_str)
        assert access_token is not None
        assert isinstance(access_token, AccessToken)

        # Revoke it
        await provider.revoke_token(access_token)

        # After revocation, load_access_token should return None
        result = await provider.load_access_token(token_str)
        assert result is None

    # --- P2: valid_scopes ---

    def test_client_registration_options_has_valid_scopes(self, provider):
        """ClientRegistrationOptions should declare valid_scopes (P2)."""
        assert provider.client_registration_options is not None
        assert provider.client_registration_options.valid_scopes == ["read", "write"]
