"""
Tests for MCP OAuth Authorization Server (mcp_server.auth).

Covers:
- RSA key management (generate, load, sign, verify)
- DCR (Dynamic Client Registration)
- Authorize endpoint with auth.enabled branching
- Consent page (allow/deny)
- Token endpoint (authorization_code, refresh_token)
- JWT claims verification (RS256, sub/scope/iss/aud/exp)
- Full end-to-end OAuth flows
"""

from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
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


def _make_code_challenge(verifier: str) -> str:
    """Create S256 code challenge from verifier."""
    return hashlib.sha256(verifier.encode()).digest().hex()


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
        from mcp_server.auth import RSAKeyManager

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
        from mcp_server.auth import RSAKeyManager

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
        from mcp_server.auth import RSAKeyManager

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

    def test_public_key_pem_returns_public_key_bytes(self, tmp_path):
        """get_public_key_pem should return the public key in PEM format."""
        from mcp_server.auth import RSAKeyManager

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
        from mcp_server.auth import RSAKeyManager

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


# ---------------------------------------------------------------------------
# OAuth Provider Unit Tests
# ---------------------------------------------------------------------------


class TestOAuthProvider:
    """Unit tests for BangumiOAuthProvider (without full server)."""

    @pytest.fixture
    def rsa_manager(self, tmp_path):
        from mcp_server.auth import RSAKeyManager

        manager = RSAKeyManager(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
        )
        manager.generate_keys()
        return manager

    @pytest.fixture
    def provider(self, rsa_manager):
        from mcp_server.auth import BangumiOAuthProvider

        return BangumiOAuthProvider(
            rsa_manager=rsa_manager,
            issuer="http://localhost:3000",
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
        from mcp.server.auth.provider import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="test-dcr-client",
            redirect_uris=["http://localhost/callback"],
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
    async def test_authorize_returns_consent_url(self, provider):
        """authorize should return a URL pointing to the consent page."""
        from mcp.server.auth.provider import (
            AuthorizationParams,
            OAuthClientInformationFull,
        )
        from pydantic import AnyUrl

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        params = AuthorizationParams(
            state="test-state",
            scopes=["read", "write"],
            code_challenge="challenge123",
            redirect_uri=AnyUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )
        url = await provider.authorize(client, params)

        # Should redirect to consent page
        assert "/consent" in url
        assert "request_token=" in url

    @pytest.mark.asyncio
    async def test_exchange_authorization_code_issues_jwt(self, provider):
        """exchange_authorization_code should return JWT + refresh token."""
        from mcp.server.auth.provider import (
            AuthorizationParams,
            OAuthClientInformationFull,
        )
        from pydantic import AnyUrl

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        params = AuthorizationParams(
            state=None,
            scopes=["read", "write"],
            code_challenge="challenge123",
            redirect_uri=AnyUrl("http://localhost/callback"),
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

        # Simulate consent allow
        auth_code = await provider.handle_consent_allow(
            request_token, username="testuser"
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
        assert decoded["iss"] == "http://localhost:3000"
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
            "iss": "http://localhost:3000",
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
            "iss": "http://localhost:3000",
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
            "iss": "http://localhost:3000",
            "aud": "bs",
            "exp": int(time.time()) + 3600,
        }
        token_str = jwt.encode(claims, other_private, algorithm="RS256")

        result = await provider.load_access_token(token_str)
        assert result is None

    @pytest.mark.asyncio
    async def test_consent_deny_clears_pending(self, provider):
        """handle_consent_deny should clear the pending auth request."""
        from mcp.server.auth.provider import (
            AuthorizationParams,
            OAuthClientInformationFull,
        )
        from pydantic import AnyUrl

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
            grant_types=["authorization_code"],
            token_endpoint_auth_method="none",
        )
        params = AuthorizationParams(
            state=None,
            scopes=["read", "write"],
            code_challenge="challenge123",
            redirect_uri=AnyUrl("http://localhost/callback"),
            redirect_uri_provided_explicitly=True,
        )
        await provider.authorize(client, params)

        # Find request_token
        request_token = next(iter(provider._pending_auths))
        assert request_token in provider._pending_auths

        # Deny
        await provider.handle_consent_deny(request_token)
        assert request_token not in provider._pending_auths


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
        from mcp_server.auth import create_auth_server

        app = create_auth_server(
            private_key_path=tmp_keys["private"],
            public_key_path=tmp_keys["public"],
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=False,
            auth_username="admin",
        )
        return app

    @pytest.fixture
    def server_app_auth_enabled(self, tmp_keys):
        """Create a test server with auth.enabled=True."""
        from mcp_server.auth import create_auth_server

        app = create_auth_server(
            private_key_path=tmp_keys["private"],
            public_key_path=tmp_keys["public"],
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=True,
            auth_username="admin",
            bs_base_url="http://bs:8000",
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
        assert data["issuer"] == "http://localhost:3000"
        assert "authorization_endpoint" in data
        assert "token_endpoint" in data
        assert "registration_endpoint" in data

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

        # Step 1: DCR
        reg_response = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost/callback"],
                "grant_types": ["authorization_code"],
                "token_endpoint_auth_method": "none",
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
        assert decoded["iss"] == "http://localhost:3000"
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

    def test_token_with_invalid_code_returns_error(self, server_app_auth_disabled):
        """Token endpoint with invalid code should return error."""
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
        assert token_response.status_code == 400
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

    @pytest.mark.asyncio
    async def test_full_flow_auth_enabled_with_mock_bs(self, server_app_auth_enabled):
        """Full OAuth flow with auth.enabled=True and mocked BS."""
        with respx.mock(base_url="http://bs:8000", using="httpcore") as mock:
            # Mock BS auth status endpoint - user is logged in
            mock.get("/api/auth/status").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {
                            "authenticated": True,
                            "user": {"username": "testuser"},
                        },
                    },
                )
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

                # Consent GET (checks BS session)
                consent_get = client.get(consent_url)
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

                # Verify JWT sub = testuser (from BS session)
                public_pem = server_app_auth_enabled.state.public_key_pem
                decoded = jwt.decode(
                    token_data["access_token"],
                    public_pem,
                    algorithms=["RS256"],
                    audience="bs",
                )
                assert decoded["sub"] == "testuser"
                assert decoded["scope"] == "read write"
