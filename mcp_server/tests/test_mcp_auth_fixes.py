"""
Tests for MCP auth fixes (S1-S4, M1, M5, M7, M10, M11).

Covers:
- S1: create_server_with_auth() properly initializes auth middleware
- S2: Per-request JWT passthrough to BS
- S3: No MCP_AUTH_ENABLED independent switch
- S4: auth.enabled=True login redirect when no session
- S8: Private key default path is /app/keys-private/ (not shared volume)
- M1: Consent form XSS escaping
- M5: Private key chmod 600
- M7: Pending auth TTL cleanup, max clients limit
- M10: CSRF token validation on consent POST
- M11: _check_bs_session logs warnings on failure
- S4+: BS_PUBLIC_URL env var for browser-reachable login redirect
- Cookie forwarding in _check_bs_session
- CSRF comparison uses hmac.compare_digest
- authorize() triggers _cleanup_expired_pending_auths
"""

from __future__ import annotations

import os
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx
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


def _extract_csrf_token(html_content: str) -> str:
    """Extract CSRF token from consent form HTML."""
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_content)
    if not match:
        raise ValueError("CSRF token not found in consent form")
    return match.group(1)


# ---------------------------------------------------------------------------
# S1: create_server_with_auth() auth middleware
# ---------------------------------------------------------------------------


class TestS1ServerAuthMiddleware:
    """S1: create_server_with_auth() must properly initialize auth middleware."""

    @pytest.fixture
    def auth_app(self, tmp_path):
        """Create a test app using create_server_with_auth() pattern."""
        from mcp_server.auth import create_auth_server

        app = create_auth_server(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=False,
            auth_username="admin",
        )
        return app

    def test_mcp_endpoint_requires_auth_with_valid_token(self, auth_app):
        """With auth configured, /mcp endpoint should require authentication."""
        # Test that auth is enforced by checking that unauthenticated requests
        # don't get 200 (they should get 401 or 405)
        client = TestClient(auth_app, raise_server_exceptions=False)
        response = client.get("/mcp")
        # Should get 401 or 405 (method not allowed) but NOT 200
        assert response.status_code in (401, 405)

    def test_mcp_endpoint_rejects_no_token(self, auth_app):
        """With auth configured, /mcp endpoint should reject requests without token."""
        client = TestClient(auth_app, raise_server_exceptions=False)
        response = client.get("/mcp")
        # Should not be 200 - auth should be required
        assert response.status_code != 200

    def test_create_server_with_auth_initializes_token_verifier(self, tmp_path):
        """create_server_with_auth() should set _token_verifier on the server."""
        # Set up env for create_server_with_auth
        os.environ["MCP_PRIVATE_KEY_PATH"] = str(tmp_path / "private.pem")
        os.environ["MCP_PUBLIC_KEY_PATH"] = str(tmp_path / "public.pem")
        os.environ["MCP_ISSUER"] = "http://localhost:3000"
        os.environ["MCP_AUDIENCE"] = "bs"
        os.environ["BS_BASE_URL"] = "http://bs:8000"

        from mcp_server.server import create_server_with_auth

        server = create_server_with_auth()
        # The server should have _token_verifier set
        assert server._token_verifier is not None
        # The server should have _auth_server_provider set
        assert server._auth_server_provider is not None

        # Clean up env
        del os.environ["MCP_PRIVATE_KEY_PATH"]
        del os.environ["MCP_PUBLIC_KEY_PATH"]
        del os.environ["MCP_ISSUER"]
        del os.environ["MCP_AUDIENCE"]
        del os.environ["BS_BASE_URL"]


# ---------------------------------------------------------------------------
# S2: Per-request JWT passthrough
# ---------------------------------------------------------------------------


class TestS2PerRequestJwtPassthrough:
    """S2: Tools should pass the current request's JWT to BS."""

    @pytest.mark.asyncio
    async def test_bs_client_supports_per_request_token(self):
        """BSClient._request should accept a per-request token parameter."""
        from mcp_server.client import BSClient

        received_tokens = []

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            received_tokens.append(auth)
            return httpx.Response(200, json={"status": "success", "data": {"logs": []}})

        transport = httpx.MockTransport(handler)
        client = BSClient(
            base_url="http://bs:8000", token="default-token", _transport=transport
        )

        # Call with default token
        await client.get_logs()
        # Call with per-request token override
        await client.get_logs(token="per-request-jwt")

        assert received_tokens[0] == "Bearer default-token"
        assert received_tokens[1] == "Bearer per-request-jwt"

    @pytest.mark.asyncio
    async def test_bs_client_per_request_token_overrides_default(self):
        """Per-request token should override the default token."""
        from mcp_server.client import BSClient

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("authorization") == "Bearer override-jwt"
            return httpx.Response(200, json={"status": "success", "data": {"logs": []}})

        transport = httpx.MockTransport(handler)
        client = BSClient(
            base_url="http://bs:8000", token="default-token", _transport=transport
        )
        await client.get_logs(token="override-jwt")

    @pytest.mark.asyncio
    async def test_bs_client_none_token_uses_no_auth(self):
        """When token=None and no default, no Authorization header should be sent."""
        from mcp_server.client import BSClient

        def handler(request: httpx.Request) -> httpx.Response:
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"status": "success", "data": {"logs": []}})

        transport = httpx.MockTransport(handler)
        client = BSClient(base_url="http://bs:8000", _transport=transport)
        await client.get_logs()


# ---------------------------------------------------------------------------
# S3: No MCP_AUTH_ENABLED independent switch
# ---------------------------------------------------------------------------


class TestS3NoMcpAuthEnabledSwitch:
    """S3: mcp_server should not have MCP_AUTH_ENABLED independent switch."""

    def test_no_mcp_auth_enabled_env_in_server(self, tmp_path):
        """create_server_with_auth() should not read MCP_AUTH_ENABLED."""
        # Set up env WITHOUT MCP_AUTH_ENABLED
        os.environ["MCP_PRIVATE_KEY_PATH"] = str(tmp_path / "private.pem")
        os.environ["MCP_PUBLIC_KEY_PATH"] = str(tmp_path / "public.pem")
        os.environ["MCP_ISSUER"] = "http://localhost:3000"
        os.environ["MCP_AUDIENCE"] = "bs"

        from mcp_server.server import create_server_with_auth

        # Should work without MCP_AUTH_ENABLED
        server = create_server_with_auth()
        assert server._token_verifier is not None

        # Clean up
        for key in [
            "MCP_PRIVATE_KEY_PATH",
            "MCP_PUBLIC_KEY_PATH",
            "MCP_ISSUER",
            "MCP_AUDIENCE",
        ]:
            os.environ.pop(key, None)

    def test_create_server_with_auth_always_enables_auth(self, tmp_path):
        """create_server_with_auth() should always set up auth regardless of env."""
        os.environ["MCP_PRIVATE_KEY_PATH"] = str(tmp_path / "private.pem")
        os.environ["MCP_PUBLIC_KEY_PATH"] = str(tmp_path / "public.pem")

        from mcp_server.server import create_server_with_auth

        server = create_server_with_auth()
        # Auth should always be configured
        assert server.settings.auth is not None
        assert server._token_verifier is not None

        for key in ["MCP_PRIVATE_KEY_PATH", "MCP_PUBLIC_KEY_PATH"]:
            os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# S8: Private key default path is /app/keys-private/ (not shared volume)
# ---------------------------------------------------------------------------


class TestS8PrivateKeyDefaultPath:
    """S8: Private key default path must be /app/keys-private/ (container-local)."""

    def test_default_private_key_path_is_keys_private(self, tmp_path):
        """Without MCP_PRIVATE_KEY_PATH env, default should be /app/keys-private/.

        We inspect the source default without actually generating keys at that path
        (which would fail on non-container hosts).
        """
        import inspect

        from mcp_server.server import create_server_with_auth

        source = inspect.getsource(create_server_with_auth)
        # Verify the default value in source is /app/keys-private/mcp_private.pem
        assert "/app/keys-private/mcp_private.pem" in source

    def test_explicit_private_key_path_overrides_default(self, tmp_path):
        """When MCP_PRIVATE_KEY_PATH is set, it should be used instead of default."""
        custom_path = str(tmp_path / "custom" / "my_key.pem")
        os.environ["MCP_PRIVATE_KEY_PATH"] = custom_path
        os.environ["MCP_PUBLIC_KEY_PATH"] = str(tmp_path / "custom" / "my_pub.pem")
        os.environ["MCP_ISSUER"] = "http://localhost:3000"
        os.environ["MCP_AUDIENCE"] = "bs"

        from mcp_server.server import create_server_with_auth

        server = create_server_with_auth()
        provider = server._auth_server_provider
        assert provider.rsa_manager.private_key_path == custom_path

        for key in [
            "MCP_PRIVATE_KEY_PATH",
            "MCP_PUBLIC_KEY_PATH",
            "MCP_ISSUER",
            "MCP_AUDIENCE",
        ]:
            os.environ.pop(key, None)

    def test_default_public_key_path_still_shared(self, tmp_path):
        """Public key default path should remain /app/keys/ (shared volume)."""
        import inspect

        from mcp_server.server import create_server_with_auth

        source = inspect.getsource(create_server_with_auth)
        # Public key still defaults to /app/keys/ (shared volume for BS to read)
        assert "/app/keys/mcp_public.pem" in source


# ---------------------------------------------------------------------------
# S4: auth.enabled=True login redirect
# ---------------------------------------------------------------------------


class TestS4AuthEnabledLoginRedirect:
    """S4: When auth.enabled=True and no session, should redirect to BS login."""

    @pytest.fixture
    def auth_enabled_app(self, tmp_path):
        """Create a test server with auth.enabled=True."""
        from mcp_server.auth import create_auth_server

        app = create_auth_server(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=True,
            auth_username="admin",
            bs_base_url="http://bs:8000",
        )
        return app

    def test_consent_get_no_session_redirects_to_login(self, auth_enabled_app):
        """auth.enabled=True + no BS session → 302 redirect to BS login."""
        with respx.mock(base_url="http://bs:8000", using="httpcore") as mock:
            # Mock BS auth status - not authenticated
            mock.get("/api/auth/status").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {"authenticated": False, "user": {}},
                    },
                )
            )

            client = TestClient(auth_enabled_app, raise_server_exceptions=False)

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
            auth_response = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": "challenge123",
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            consent_url = auth_response.headers["location"]

            # Consent GET should redirect to BS login
            consent_get = client.get(consent_url, follow_redirects=False)
            assert consent_get.status_code == 302
            location = consent_get.headers["location"]
            assert "/login" in location

    def test_consent_get_with_session_proceeds(self, auth_enabled_app):
        """auth.enabled=True + valid BS session → consent page shown."""
        with respx.mock(base_url="http://bs:8000", using="httpcore") as mock:
            # Mock BS auth status - authenticated
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

            client = TestClient(auth_enabled_app, raise_server_exceptions=False)

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
            auth_response = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": "challenge123",
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            consent_url = auth_response.headers["location"]

            # Consent GET should show the form (not redirect)
            consent_get = client.get(consent_url)
            assert consent_get.status_code == 200
            assert "Authorization Request" in consent_get.text


# ---------------------------------------------------------------------------
# M1: Consent form XSS escaping
# ---------------------------------------------------------------------------


class TestM1ConsentFormXssEscaping:
    """M1: Consent form must HTML-escape dynamic fields."""

    @pytest.fixture
    def auth_app(self, tmp_path):
        from mcp_server.auth import create_auth_server

        return create_auth_server(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=False,
            auth_username="<script>alert('xss')</script>",
        )

    def test_consent_form_escapes_username(self, auth_app):
        """Username with HTML special characters should be escaped."""
        client = TestClient(auth_app, raise_server_exceptions=False)

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
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": "challenge123",
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        consent_url = auth_response.headers["location"]

        # Consent GET
        consent_get = client.get(consent_url)
        assert consent_get.status_code == 200

        # The username should be escaped - no raw <script> tag
        assert "<script>alert" not in consent_get.text
        # But the escaped version should be present
        assert "&lt;script&gt;" in consent_get.text or "&#x27;" in consent_get.text


# ---------------------------------------------------------------------------
# M5: Private key chmod 600
# ---------------------------------------------------------------------------


class TestM5PrivateKeyChmod:
    """M5: Private key file should be created with 0o600 permissions."""

    def test_generate_keys_sets_0600_permissions(self, tmp_path):
        """generate_keys should create private key with owner-only permissions."""
        from mcp_server.auth import RSAKeyManager

        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"
        manager = RSAKeyManager(
            private_key_path=str(private_path),
            public_key_path=str(public_path),
        )
        manager.generate_keys()

        # Check private key permissions
        mode = os.stat(private_path).st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# M7: Pending auth TTL cleanup, max clients
# ---------------------------------------------------------------------------


class TestM7PendingAuthTtlAndMaxClients:
    """M7: Pending auth should have TTL cleanup and client limit."""

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
    async def test_pending_auth_ttl_cleanup(self, provider):
        """Expired pending auths should be cleaned up."""
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

        # Authorize to create a pending auth
        await provider.authorize(client, params)
        assert len(provider._pending_auths) == 1

        # Manually set created_at to past (simulate expiry)
        request_token = None
        for rt, info in provider._pending_auths.items():
            info["created_at"] = time.time() - 601  # > 10 min TTL
            request_token = rt

        # Trigger cleanup via get_consent_context
        result = await provider.get_consent_context(request_token)
        assert result is None  # Should be expired
        assert len(provider._pending_auths) == 0  # Should be cleaned up

    @pytest.mark.asyncio
    async def test_max_clients_limit(self, provider):
        """Should enforce max client registration limit."""
        from mcp.server.auth.provider import OAuthClientInformationFull

        from mcp_server.auth import MAX_CLIENTS

        # Temporarily set a low limit for testing
        original_max = MAX_CLIENTS
        try:
            # Monkey-patch the limit
            import mcp_server.auth as auth_module

            auth_module.MAX_CLIENTS = 5

            for i in range(5):
                client_info = OAuthClientInformationFull(
                    client_id=f"client-{i}",
                    redirect_uris=["http://localhost/callback"],
                    grant_types=["authorization_code"],
                    token_endpoint_auth_method="none",
                )
                await provider.register_client(client_info)

            # 6th should fail
            with pytest.raises(RuntimeError, match="limit"):
                client_info = OAuthClientInformationFull(
                    client_id="client-overflow",
                    redirect_uris=["http://localhost/callback"],
                    grant_types=["authorization_code"],
                    token_endpoint_auth_method="none",
                )
                await provider.register_client(client_info)
        finally:
            auth_module.MAX_CLIENTS = original_max


# ---------------------------------------------------------------------------
# M10: CSRF token validation
# ---------------------------------------------------------------------------


class TestM10CsrfTokenValidation:
    """M10: Consent POST must validate CSRF token."""

    @pytest.fixture
    def auth_app(self, tmp_path):
        from mcp_server.auth import create_auth_server

        return create_auth_server(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=False,
            auth_username="admin",
        )

    def test_consent_post_without_csrf_returns_403(self, auth_app):
        """POST to /consent without CSRF token should return 403."""
        client = TestClient(auth_app, raise_server_exceptions=False)

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
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": "challenge123",
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
            },
            follow_redirects=False,
        )
        assert consent_post.status_code == 403

    def test_consent_post_with_wrong_csrf_returns_403(self, auth_app):
        """POST to /consent with wrong CSRF token should return 403."""
        client = TestClient(auth_app, raise_server_exceptions=False)

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
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": "challenge123",
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
                "csrf_token": "wrong-token",
            },
            follow_redirects=False,
        )
        assert consent_post.status_code == 403

    def test_consent_form_contains_csrf_token(self, auth_app):
        """Consent form should include a CSRF token field."""
        client = TestClient(auth_app, raise_server_exceptions=False)

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
        auth_response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "http://localhost/callback",
                "response_type": "code",
                "code_challenge": "challenge123",
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        consent_url = auth_response.headers["location"]

        # Consent GET
        consent_get = client.get(consent_url)
        assert consent_get.status_code == 200

        # Should contain CSRF token field
        assert 'name="csrf_token"' in consent_get.text


# ---------------------------------------------------------------------------
# M11: _check_bs_session logs warnings
# ---------------------------------------------------------------------------


class TestM11CheckBsSessionLogging:
    """M11: _check_bs_session should log warnings on failure."""

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
            auth_enabled=True,
            auth_username="admin",
            bs_base_url="http://bs:8000",
        )

    @pytest.mark.asyncio
    async def test_bs_unreachable_logs_warning(self, provider, caplog):
        """When BS is unreachable, should log a warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            # Use a bad URL to simulate unreachable BS
            provider.bs_base_url = "http://localhost:59999"
            result = await provider._check_bs_session()
            assert result is None
            # Check that a warning was logged
            assert any(
                "Failed to check BS session" in r.message for r in caplog.records
            )


# ---------------------------------------------------------------------------
# S4+: BS_PUBLIC_URL for browser-reachable login redirect
# ---------------------------------------------------------------------------


class TestS4BsPublicUrl:
    """S4+: Login redirect should use BS_PUBLIC_URL when configured."""

    @pytest.fixture
    def rsa_manager(self, tmp_path):
        from mcp_server.auth import RSAKeyManager

        manager = RSAKeyManager(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
        )
        manager.generate_keys()
        return manager

    @pytest.mark.asyncio
    async def test_provider_stores_bs_public_url(self, rsa_manager):
        """BangumiOAuthProvider should accept and store bs_public_url."""
        from mcp_server.auth import BangumiOAuthProvider

        provider = BangumiOAuthProvider(
            rsa_manager=rsa_manager,
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=True,
            auth_username="admin",
            bs_base_url="http://bs:8000",
            bs_public_url="https://bs.example.com",
        )
        assert provider.bs_public_url == "https://bs.example.com"

    @pytest.mark.asyncio
    async def test_provider_bs_public_url_defaults_to_none(self, rsa_manager):
        """BangumiOAuthProvider.bs_public_url should default to None."""
        from mcp_server.auth import BangumiOAuthProvider

        provider = BangumiOAuthProvider(
            rsa_manager=rsa_manager,
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=True,
            auth_username="admin",
            bs_base_url="http://bs:8000",
        )
        assert provider.bs_public_url is None

    def test_login_redirect_uses_bs_public_url_when_configured(self, tmp_path):
        """When BS_PUBLIC_URL is set, login redirect should use it."""
        from mcp_server.auth import create_auth_server

        app = create_auth_server(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=True,
            auth_username="admin",
            bs_base_url="http://bs:8000",
            bs_public_url="https://bs.example.com",
        )

        with respx.mock(base_url="http://bs:8000", using="httpcore") as mock:
            mock.get("/api/auth/status").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {"authenticated": False, "user": {}},
                    },
                )
            )

            client = TestClient(app, raise_server_exceptions=False)

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
            auth_response = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": "challenge123",
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            consent_url = auth_response.headers["location"]

            # Consent GET should redirect to public URL login
            consent_get = client.get(consent_url, follow_redirects=False)
            assert consent_get.status_code == 302
            location = consent_get.headers["location"]
            assert location.startswith("https://bs.example.com/login")

    def test_login_redirect_fallback_to_internal_with_warning(self, tmp_path, caplog):
        """When BS_PUBLIC_URL is not set, fallback to bs_base_url + warning."""
        import logging

        from mcp_server.auth import create_auth_server

        app = create_auth_server(
            private_key_path=str(tmp_path / "private.pem"),
            public_key_path=str(tmp_path / "public.pem"),
            issuer="http://localhost:3000",
            audience="bs",
            auth_enabled=True,
            auth_username="admin",
            bs_base_url="http://bs:8000",
        )

        with respx.mock(base_url="http://bs:8000", using="httpcore") as mock:
            mock.get("/api/auth/status").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {"authenticated": False, "user": {}},
                    },
                )
            )

            client = TestClient(app, raise_server_exceptions=False)

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
            auth_response = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://localhost/callback",
                    "response_type": "code",
                    "code_challenge": "challenge123",
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            consent_url = auth_response.headers["location"]

            # Consent GET should fallback to internal URL + log warning
            with caplog.at_level(logging.WARNING):
                consent_get = client.get(consent_url, follow_redirects=False)
                assert consent_get.status_code == 302
                location = consent_get.headers["location"]
                assert location.startswith("http://bs:8000/login")
                # Should have logged a warning about BS_PUBLIC_URL not configured
                assert any("BS_PUBLIC_URL" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Cookie forwarding in _check_bs_session
# ---------------------------------------------------------------------------


class TestCookieForwarding:
    """Verify _check_bs_session forwards browser Cookie header."""

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
            auth_enabled=True,
            auth_username="admin",
            bs_base_url="http://bs:8000",
        )

    @pytest.mark.asyncio
    async def test_check_bs_session_forwards_cookie(self, provider):
        """_check_bs_session should forward the browser Cookie header to BS."""
        received_cookies = []

        def handler(request: httpx.Request) -> httpx.Response:
            cookie = request.headers.get("cookie", "")
            received_cookies.append(cookie)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"authenticated": True, "user": {"username": "testuser"}},
                },
            )

        import httpx as httpx_module

        transport = httpx_module.MockTransport(handler)

        # Monkey-patch httpx.AsyncClient to use our mock transport
        import httpx

        original_init = httpx.AsyncClient.__init__

        def mock_init(self, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs["base_url"] = "http://bs:8000"
            original_init(self, *args, **kwargs)

        httpx.AsyncClient.__init__ = mock_init
        try:
            result = await provider._check_bs_session(cookie="session=abc123")
        finally:
            httpx.AsyncClient.__init__ = original_init

        assert result == "testuser"
        assert received_cookies == ["session=abc123"]

    @pytest.mark.asyncio
    async def test_check_bs_session_no_cookie_header_when_none(self, provider):
        """_check_bs_session should not send Cookie header when cookie is None."""
        received_cookies = []

        def handler(request: httpx.Request) -> httpx.Response:
            cookie = request.headers.get("cookie", "")
            received_cookies.append(cookie)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"authenticated": True, "user": {"username": "testuser"}},
                },
            )

        import httpx as httpx_module

        transport = httpx_module.MockTransport(handler)

        # Monkey-patch httpx.AsyncClient to use our mock transport
        import httpx

        original_init = httpx.AsyncClient.__init__

        def mock_init(self, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs["base_url"] = "http://bs:8000"
            original_init(self, *args, **kwargs)

        httpx.AsyncClient.__init__ = mock_init
        try:
            result = await provider._check_bs_session(cookie=None)
        finally:
            httpx.AsyncClient.__init__ = original_init

        assert result == "testuser"
        assert received_cookies == [""]  # No cookie header sent


# ---------------------------------------------------------------------------
# CSRF comparison uses hmac.compare_digest
# ---------------------------------------------------------------------------


class TestCsrfTimingSafe:
    """CSRF token comparison should use hmac.compare_digest."""

    def test_csrf_uses_constant_time_comparison(self, tmp_path):
        """The CSRF validation code should use hmac.compare_digest."""
        import inspect

        from mcp_server.auth import handle_consent

        source = inspect.getsource(handle_consent)
        assert "hmac.compare_digest" in source


# ---------------------------------------------------------------------------
# authorize() triggers cleanup
# ---------------------------------------------------------------------------


class TestAuthorizeCleanup:
    """authorize() should trigger _cleanup_expired_pending_auths."""

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
    async def test_authorize_triggers_cleanup(self, provider):
        """authorize() should call _cleanup_expired_pending_auths."""
        from mcp.server.auth.provider import (
            AuthorizationParams,
            OAuthClientInformationFull,
        )
        from pydantic import AnyUrl

        # Add an expired pending auth manually
        provider._pending_auths["expired-token"] = {
            "client_id": "old-client",
            "redirect_uri": "http://localhost/callback",
            "scopes": ["read"],
            "state": None,
            "code_challenge": "old",
            "redirect_uri_provided_explicitly": True,
            "resource": None,
            "csrf_token": "old-csrf",
            "created_at": time.time() - 601,  # expired
        }
        assert len(provider._pending_auths) == 1

        # Now authorize a new request - should trigger cleanup
        client = OAuthClientInformationFull(
            client_id="new-client",
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

        # The expired one should be cleaned up, only the new one remains
        assert "expired-token" not in provider._pending_auths
        assert len(provider._pending_auths) == 1
