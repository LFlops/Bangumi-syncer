"""MCP server entry point with Streamable HTTP transport.

Supports ``python -m mcp_server.server`` to start the server.
"""

from __future__ import annotations

import os

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer

from mcp_server.client import BSClient

# ---------------------------------------------------------------------------
# Server singleton
# ---------------------------------------------------------------------------

_server: MCPServer | None = None


def _get_bs_base_url() -> str:
    """Get BS base URL from environment."""
    return os.environ.get("BS_BASE_URL", "http://bs:8000")


def _create_client() -> BSClient:
    """Create a BSClient from environment settings.

    trust_env=False: this client is explicitly configured via BS_BASE_URL
    and should not pick up ambient HTTP(S)_PROXY from the host environment
    (container-to-container calls don't need outbound proxy).
    """
    return BSClient(
        base_url=_get_bs_base_url(),
        token=os.environ.get("BS_API_TOKEN", ""),
        trust_env=False,
    )


def _register_tools(server: MCPServer) -> None:
    """Register the 3 MCP tools on the given server.

    Tools no longer capture a shared _client; instead they retrieve
    the current access token per-request from the MCP auth context.
    """

    @server.tool(
        name="get_logs",
        description=(
            "Fetch logs from Bangumi-syncer. Filter by level (info/warn/error), "
            "search keyword, time range (since/until in ISO format), and limit."
        ),
    )
    async def get_logs(
        level: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> str:
        from mcp_server.tools import get_logs_tool

        return await get_logs_tool(
            level=level,
            search=search,
            limit=limit,
            since=since,
            until=until,
        )

    @server.tool(
        name="get_current_config",
        description="Fetch the current Bangumi-syncer configuration.",
    )
    async def get_current_config() -> str:
        from mcp_server.tools import get_current_config_tool

        return await get_current_config_tool()

    @server.tool(
        name="update_config",
        description=(
            "Update a Bangumi-syncer configuration value. "
            "Requires section, key, and value."
        ),
    )
    async def update_config(
        section: str,
        key: str,
        value: str,
    ) -> str:
        from mcp_server.tools import update_config_tool

        return await update_config_tool(section=section, key=key, value=value)


def create_server() -> MCPServer:
    """Create and configure the MCP server with 3 tools registered (no auth)."""
    server = MCPServer(name="bangumi-syncer-mcp")
    _register_tools(server)
    return server


def get_server() -> MCPServer:
    """Get or create the singleton server instance (no auth)."""
    global _server
    if _server is None:
        _server = create_server()
    return _server


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def create_server_with_auth() -> MCPServer:
    """Create server with OAuth auth enabled (production entry point).

    S1 fix: Pass auth_server_provider to MCPServer constructor (not post-construction
    assignment) so that _token_verifier is properly initialized and the streamable
    HTTP app gets AuthenticationMiddleware/AuthContextMiddleware/RequireAuthMiddleware.

    S3 fix: No MCP_AUTH_ENABLED independent switch. Auth is always configured;
    auth.enabled is determined by the provider (which reads from BS at runtime).

    S8 fix: Private key defaults to /app/keys-private/ (container-local, not shared volume).

    Reads configuration from environment:
    - MCP_AUTH_USERNAME: username for auth.enabled=False (default: admin)
    - MCP_PRIVATE_KEY_PATH: path to RSA private key PEM (default: /app/keys-private/mcp_private.pem)
    - MCP_PUBLIC_KEY_PATH: path to RSA public key PEM (shared with BS)
    - MCP_TOKEN_EXPIRY_SECONDS: JWT lifetime (default: 3600)
    - MCP_ISSUER: OAuth issuer URL (default: http://localhost:3000)
    - MCP_AUDIENCE: JWT audience (default: bs)
    - BS_BASE_URL: BS base URL for session checks (default: http://bs:8000)
    - BS_PUBLIC_URL: Browser-reachable BS URL for login redirect (optional)
    """
    from mcp_server.auth import BangumiOAuthProvider, RSAKeyManager

    # S8: Default private key to container-local path (not shared volume)
    private_key_path = os.environ.get(
        "MCP_PRIVATE_KEY_PATH", "/app/keys-private/mcp_private.pem"
    )
    public_key_path = os.environ.get("MCP_PUBLIC_KEY_PATH", "/app/keys/mcp_public.pem")

    rsa_manager = RSAKeyManager(
        private_key_path=private_key_path,
        public_key_path=public_key_path,
    )
    rsa_manager.load_or_generate()

    provider = BangumiOAuthProvider(
        rsa_manager=rsa_manager,
        issuer=os.environ.get("MCP_ISSUER", "http://localhost:3000"),
        audience=os.environ.get("MCP_AUDIENCE", "bs"),
        token_expiry_seconds=int(os.environ.get("MCP_TOKEN_EXPIRY_SECONDS", "3600")),
        auth_enabled=True,  # Provider handles auth.enabled branching internally
        auth_username=os.environ.get("MCP_AUTH_USERNAME", "admin"),
        bs_base_url=_get_bs_base_url(),
        bs_public_url=os.environ.get("BS_PUBLIC_URL"),
    )

    # S1 fix: Create MCPServer with auth_server_provider in constructor
    # This ensures _token_verifier is initialized via ProviderTokenVerifier
    server = MCPServer(
        name="bangumi-syncer-mcp",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=provider.issuer,
            resource_server_url=f"{provider.issuer}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            required_scopes=["read", "write"],
        ),
    )

    # Register tools
    _register_tools(server)

    # Register consent route
    @server.custom_route("/consent", methods=["GET", "POST"])
    async def consent_route(request):
        from mcp_server.auth import handle_consent

        return await handle_consent(request, provider)

    return server


def main() -> None:
    """Start the MCP server on Streamable HTTP (default port 3000)."""
    port = int(os.environ.get("MCP_PORT", "3000"))
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    server = create_server_with_auth()
    server.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
