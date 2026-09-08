"""FastMCP OAuthProvider implementation for Bangumi-syncer.

Implements:
- RSA key management (RS256 JWT signing)
- OAuthProvider (FastMCP 4) with authorize/token/register
- auth.enabled branching (BS session check via security_manager vs configured username)
- Consent page (allow/deny) with CSRF protection
- Dynamic Client Registration (RFC 7591)
- CIMD (Client ID Metadata Document) integration
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
    RefreshToken,
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

# Constants
MAX_CLIENTS = 1000
PENDING_AUTH_TTL = 600  # 10 minutes
AUTH_CODE_TTL = 300  # 5 minutes


class RSAKeyManager:
    """Manages RSA key pair for JWT signing (RS256)."""

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
        """Generate a new RSA key pair and save to disk."""
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
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        # Ensure directories exist
        os.makedirs(os.path.dirname(self.private_key_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(self.public_key_path) or ".", exist_ok=True)

        with open(self.private_key_path, "wb") as f:
            f.write(private_pem)
        # Set private key to owner-only read/write (chmod 600)
        os.chmod(self.private_key_path, 0o600)
        with open(self.public_key_path, "wb") as f:
            f.write(public_pem)

    def load_or_generate(self) -> None:
        """Load existing keys from disk, or generate new ones if not found."""
        if os.path.exists(self.private_key_path) and os.path.exists(
            self.public_key_path
        ):
            self._load_keys()
        else:
            self.generate_keys()

    def _load_keys(self) -> None:
        """Load keys from disk."""
        with open(self.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )
        with open(self.public_key_path, "rb") as f:
            self._public_key = serialization.load_pem_public_key(f.read())

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
        """Get private key in PEM format."""
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def get_public_key_pem(self) -> bytes:
        """Get public key in PEM format."""
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def sign_jwt(self, claims: dict[str, Any]) -> str:
        """Sign a JWT with RS256. Adds jti for uniqueness if not present."""
        if "jti" not in claims:
            claims["jti"] = secrets.token_urlsafe(16)
        return jwt.encode(claims, self.get_private_key_pem(), algorithm="RS256")

    def verify_jwt(
        self, token: str, audience: str | None = None
    ) -> dict[str, Any] | None:
        """Verify a JWT with the public key. Returns claims or None if invalid."""
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
    """OAuth Authorization Server Provider for Bangumi-syncer.

    Implements the FastMCP 4 OAuthProvider with:
    - CIMD (Client ID Metadata Document) support via CIMDClientManager
    - auth.enabled branching (BS session check vs configured username)
    - Consent page flow with CSRF protection
    - JWT token issuance (RS256)
    - Dynamic Client Registration (RFC 7591)
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
        client_registration_options: ClientRegistrationOptions | None = None,
        revocation_options: RevocationOptions | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            issuer_url=issuer,
            client_registration_options=client_registration_options
            or ClientRegistrationOptions(enabled=True, valid_scopes=["read", "write"]),
            revocation_options=revocation_options or RevocationOptions(enabled=True),
        )
        self.rsa_manager = rsa_manager
        self.issuer = issuer
        self.audience = audience
        self.token_expiry_seconds = token_expiry_seconds
        self.auth_enabled = auth_enabled
        self.auth_username = auth_username

        # Default scopes for issued tokens
        self._default_scopes = ["read", "write"]
        self.MAX_CLIENTS = MAX_CLIENTS

        # In-memory stores
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._pending_auths: dict[str, dict[str, Any]] = {}
        self._revoked_tokens: set[str] = set()

        # CIMD manager with default scope injection
        self.cimd = CIMDClientManager(
            enable_cimd=True,
            default_scope=" ".join(self._default_scopes),
        )

    # ------------------------------------------------------------------
    # OAuthProvider interface
    # ------------------------------------------------------------------

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """Override to inject client_id_metadata_document_supported=True."""
        routes = super().get_routes(mcp_path)
        # Rebuild metadata with CIMD support advertised
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
        """Retrieve client by ID.

        CIMD branch: URL client_id → CIMDClientManager
        DCR branch: static registry
        """
        # CIMD branch: URL client_id
        if self.cimd.is_cimd_client_id(client_id):
            return await self.cimd.get_client(client_id)
        # DCR fallback: static registry
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a new client (DCR). Enforces max client limit."""
        if not client_info.client_id:
            client_info.client_id = secrets.token_urlsafe(16)
        if (
            client_info.token_endpoint_auth_method != "none"
            and not client_info.client_secret
        ):
            client_info.client_secret = secrets.token_hex(32)
        # Assign default scope if not specified
        if not client_info.scope:
            client_info.scope = " ".join(self._default_scopes)
        # Enforce max client limit
        if len(self._clients) >= self.MAX_CLIENTS:
            raise RuntimeError(
                f"Client registration limit reached ({self.MAX_CLIENTS}). "
                "Cannot register new clients."
            )
        self._clients[client_info.client_id] = client_info

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Handle /authorize - store pending request and return consent URL.

        Validates that the client is registered and redirect_uri matches.
        """
        # Validate client is known
        client_id = client.client_id
        if self.cimd.is_cimd_client_id(client_id):
            # CIMD clients are validated via CIMD, skip registry check
            pass
        elif client_id not in self._clients:
            raise ValueError(f"Unknown client: {client_id}")

        # Validate redirect_uri
        redirect_uri_str = str(params.redirect_uri)
        if params.redirect_uri_provided_explicitly:
            if self.cimd.is_cimd_client_id(client_id):
                # P0-1: CIMD clients MUST have redirect_uri validated against
                # their CIMD document's redirect_uris via component-level matching
                cimd_client = await self.cimd.get_client(client_id)
                if (
                    cimd_client is not None
                    and hasattr(cimd_client, "cimd_document")
                    and cimd_client.cimd_document is not None
                ):
                    # validate_redirect_uri lives on CIMDFetcher (self.cimd._fetcher)
                    if not self.cimd._fetcher.validate_redirect_uri(
                        cimd_client.cimd_document, redirect_uri_str
                    ):
                        raise ValueError(
                            f"redirect_uri mismatch: {redirect_uri_str} not in CIMD document redirect_uris"
                        )
                else:
                    raise ValueError(
                        f"redirect_uri validation failed: cannot resolve CIMD document for {client_id}"
                    )
            elif client_id in self._clients:
                # P0-2: DCR clients use component-level exact matching
                registered_client = self._clients[client_id]
                allowed_uris = [str(u) for u in (registered_client.redirect_uris or [])]
                if allowed_uris and not self._matches_redirect_uri(
                    redirect_uri_str, allowed_uris
                ):
                    raise ValueError(
                        f"redirect_uri mismatch: {redirect_uri_str} not in registered URIs"
                    )

        # Trigger cleanup of expired pending auths to prevent unbounded growth
        self._cleanup_expired_pending_auths()
        request_token = secrets.token_urlsafe(32)
        # Generate CSRF token bound to this pending auth
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
        """Component-level exact matching for redirect URIs.

        Compares (scheme, netloc, path) components. For loopback hosts
        (localhost/127.0.0.1), port flexibility is allowed per RFC 8252 §7.3.
        """
        parsed = urlsplit(redirect_uri)
        for allowed in allowed_uris:
            allowed_parsed = urlsplit(allowed)
            # Scheme must match exactly
            if parsed.scheme != allowed_parsed.scheme:
                continue
            # Path must match exactly
            if parsed.path.rstrip("/") != allowed_parsed.path.rstrip("/"):
                continue
            # Host must match exactly
            if parsed.hostname != allowed_parsed.hostname:
                continue
            # Port: allow flexibility only for loopback hosts
            if is_loopback_host(parsed.hostname):
                return True
            # Non-loopback: port must match exactly
            if parsed.port == allowed_parsed.port:
                return True
        return False

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        """Load authorization code by its string."""
        return self._auth_codes.get(authorization_code)

    def _build_jwt_claims(
        self,
        subject: str,
        scopes: list[str],
        resource: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Build JWT claims and sign. Returns (claims, access_token)."""
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
        return claims, self.rsa_manager.sign_jwt(claims)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Exchange authorization code for access token + refresh token."""
        _, access_token = self._build_jwt_claims(
            subject=authorization_code.subject or self.auth_username,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )
        refresh_token_str = secrets.token_urlsafe(32)

        # Store refresh token
        refresh_token = RefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            subject=authorization_code.subject,
        )
        self._refresh_tokens[refresh_token_str] = refresh_token

        # Clean up auth code (one-time use)
        self._auth_codes.pop(authorization_code.code, None)

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=self.token_expiry_seconds,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh_token_str,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Verify JWT access token and return AccessToken.

        Also checks the token's jti against the revocation set (P1-2).
        """
        claims = self.rsa_manager.verify_jwt(token, audience=self.audience)
        if claims is None:
            return None

        # P1-2: Check if this token has been revoked
        jti = claims.get("jti")
        if jti and jti in self._revoked_tokens:
            return None

        return AccessToken(
            token=token,
            client_id="",
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
        """Load refresh token by its string."""
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
        """Exchange refresh token for new access token + refresh token (rotation)."""
        _, access_token = self._build_jwt_claims(
            subject=refresh_token.subject or self.auth_username,
            scopes=scopes,
        )

        # Rotate refresh token (new one)
        new_refresh_token_str = secrets.token_urlsafe(32)
        new_refresh_token = RefreshToken(
            token=new_refresh_token_str,
            client_id=client.client_id,
            scopes=scopes,
            subject=refresh_token.subject,
        )
        self._refresh_tokens[new_refresh_token_str] = new_refresh_token

        # Remove old refresh token
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
        """Revoke an access or refresh token.

        For AccessToken: records the jti in the revocation set (P1-2).
        For RefreshToken: removes from the refresh tokens store.
        """
        if isinstance(token, AccessToken):
            # P1-2: Record jti in revocation set so verify_token rejects it
            jti = token.claims.get("jti")
            if jti:
                self._revoked_tokens.add(jti)
        elif isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)

    # ------------------------------------------------------------------
    # Consent flow helpers
    # ------------------------------------------------------------------

    def _cleanup_expired_pending_auths(self) -> None:
        """Remove expired pending auth requests (TTL enforcement)."""
        now = time.time()
        expired = [
            rt
            for rt, info in self._pending_auths.items()
            if now - info.get("created_at", 0) > PENDING_AUTH_TTL
        ]
        for rt in expired:
            del self._pending_auths[rt]

    async def get_consent_context(
        self, request_token: str, session_token: str | None = None
    ) -> dict[str, Any] | None:
        """Get context for the consent page.

        Args:
            request_token: The pending auth request token.
            session_token: Optional session token to validate via security_manager.
        """
        self._cleanup_expired_pending_auths()
        pending = self._pending_auths.get(request_token)
        if pending is None:
            return None

        # Determine username based on auth.enabled
        username = self.auth_username
        if self.auth_enabled:
            # Check BS session via security_manager (same process, not HTTP)
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
        """Handle consent allow - validate CSRF and generate authorization code.

        Args:
            request_token: The pending auth request token.
            username: Optional username override.
            csrf_token: CSRF token from the form submission.
        """
        self._cleanup_expired_pending_auths()
        pending_info = self._pending_auths.get(request_token)
        if pending_info is None:
            raise ValueError("Invalid or expired request_token")

        # Validate CSRF token (one-time use, bound to pending auth)
        expected_csrf = pending_info.get("csrf_token", "")
        if not csrf_token or not hmac.compare_digest(
            str(csrf_token), str(expected_csrf)
        ):
            raise ValueError("Invalid CSRF token")

        auth_code = secrets.token_urlsafe(32)
        code_obj = AuthorizationCode(
            code=auth_code,
            scopes=pending_info["scopes"],
            expires_at=time.time() + AUTH_CODE_TTL,  # 5 min expiry
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

        # Clean up pending
        del self._pending_auths[request_token]

        return auth_code

    async def handle_consent_deny(self, request_token: str) -> None:
        """Handle consent deny - clean up pending request."""
        self._pending_auths.pop(request_token, None)

    def _render_consent_form(self, context: dict[str, Any]) -> str:
        """Render HTML consent form with proper HTML escaping (XSS prevention)."""
        # Escape all dynamic fields to prevent XSS
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
# Server factory
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
    """Create a full auth-enabled Starlette app for testing.

    Returns a Starlette app with OAuth endpoints (authorize, token, register)
    and consent page.
    """
    from fastmcp import FastMCP
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

    # Initialize RSA keys
    rsa_manager = RSAKeyManager(
        private_key_path=private_key_path,
        public_key_path=public_key_path,
    )
    rsa_manager.load_or_generate()

    # Create provider
    provider = BangumiOAuthProvider(
        base_url=base_url or issuer,
        rsa_manager=rsa_manager,
        issuer=issuer,
        audience=audience,
        token_expiry_seconds=token_expiry_seconds,
        auth_enabled=auth_enabled,
        auth_username=auth_username,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["read", "write"]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )

    # Create MCP server with auth
    mcp = FastMCP(name="bangumi-syncer-mcp", auth=provider)

    # Register consent route BEFORE calling http_app
    @mcp.custom_route("/consent", methods=["GET", "POST"])
    async def consent_route(request: Request) -> Response:
        return await handle_consent(request, provider)

    # Get the Starlette app
    app = mcp.http_app(path="/mcp")

    # Store public key and provider on app state for testing
    app.state.public_key_pem = rsa_manager.get_public_key_pem()
    app.state.provider = provider

    return app


async def handle_consent(request: Request, provider: BangumiOAuthProvider) -> Response:
    """Handle consent page GET/POST. Usable as a custom_route handler."""
    # Get request_token from query (GET) or form (POST)
    if request.method == "GET":
        request_token = request.query_params.get("request_token")
    else:
        form = await request.form()
        request_token = form.get("request_token")

    if not request_token:
        return HTMLResponse("<h1>Error: missing request_token</h1>", status_code=400)

    # Extract session token from cookie
    session_token = None
    cookie = request.headers.get("cookie")
    if cookie:
        # Parse cookie to find session token
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("session_token="):
                session_token = part.split("=", 1)[1]
                break

    if request.method == "GET":
        # When auth.enabled=True, check session first
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

    # POST - Validate CSRF token
    form = await request.form()
    action = form.get("action", "deny")
    rt = str(request_token)
    submitted_csrf = form.get("csrf_token")

    # Look up pending auth BEFORE any deletion
    pending_info = provider._pending_auths.get(rt)
    if pending_info is None:
        return HTMLResponse(
            "<h1>Error: invalid or expired request</h1>", status_code=400
        )

    # Validate CSRF token (one-time use, bound to pending auth)
    expected_csrf = pending_info.get("csrf_token", "")
    if not submitted_csrf or not hmac.compare_digest(
        str(submitted_csrf), str(expected_csrf)
    ):
        return HTMLResponse("<h1>Error: invalid CSRF token</h1>", status_code=403)

    redirect_uri = pending_info["redirect_uri"]
    state = pending_info.get("state")

    if action == "allow":
        # When auth_enabled=True, re-check session on POST allow
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
