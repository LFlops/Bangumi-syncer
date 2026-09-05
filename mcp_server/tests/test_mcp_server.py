"""
Tests for MCP server entry point and tool registration (mcp_server.server, mcp_server.tools).

Verifies:
- server exposes Streamable HTTP entry point
- 3 tools are registered with correct names/descriptions/schemas
"""

import pytest

from mcp_server.server import create_server, get_server

# ---------------------------------------------------------------------------
# Server creation
# ---------------------------------------------------------------------------


class TestServerCreation:
    """Server factory should produce a valid MCP server with 3 tools registered."""

    def test_create_server_returns_server_instance(self):
        """create_server should return a server object that has a tool registry."""
        server = create_server()
        assert server is not None
        # FastMCP/Server exposes a method to list tools
        assert hasattr(server, "list_tools") or hasattr(server, "_tool_manager")

    def test_get_server_is_singleton(self):
        """get_server should return the same instance on repeated calls."""
        s1 = get_server()
        s2 = get_server()
        assert s1 is s2


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """The 3 tools must be registered with correct names and schemas."""

    @pytest.fixture
    def tools(self):
        """Fetch the list of registered tools from the server."""
        server = get_server()

        # MCP SDK v2 (MCPServer): tools accessible via _tool_manager.list_tools()
        if hasattr(server, "_tool_manager"):
            return server._tool_manager.list_tools()
        # Fallback: low-level Server async API
        import asyncio

        return asyncio.run(server.list_tools())

    @pytest.fixture
    def tool_names(self, tools):
        return {t.name for t in tools}

    def test_get_logs_registered(self, tool_names):
        assert "get_logs" in tool_names

    def test_get_current_config_registered(self, tool_names):
        assert "get_current_config" in tool_names

    def test_update_config_registered(self, tool_names):
        assert "update_config" in tool_names

    def test_exactly_three_tools(self, tools):
        assert len(tools) == 3

    # --- get_logs schema ---

    def test_get_logs_has_description(self, tools):
        tool = next(t for t in tools if t.name == "get_logs")
        assert tool.description
        assert len(tool.description) > 10

    def test_get_logs_input_schema_has_expected_params(self, tools):
        tool = next(t for t in tools if t.name == "get_logs")
        props = tool.parameters.get("properties", {})
        assert "level" in props
        assert "search" in props
        assert "limit" in props
        assert "since" in props
        assert "until" in props

    def test_get_logs_no_required_params(self, tools):
        """All get_logs params should be optional (no required fields)."""
        tool = next(t for t in tools if t.name == "get_logs")
        required = tool.parameters.get("required", [])
        assert required == [] or required is None

    # --- get_current_config schema ---

    def test_get_current_config_no_params(self, tools):
        tool = next(t for t in tools if t.name == "get_current_config")
        props = tool.parameters.get("properties", {})
        assert len(props) == 0

    def test_get_current_config_has_description(self, tools):
        tool = next(t for t in tools if t.name == "get_current_config")
        assert tool.description
        assert len(tool.description) > 10

    # --- update_config schema ---

    def test_update_config_has_required_params(self, tools):
        tool = next(t for t in tools if t.name == "update_config")
        required = tool.parameters.get("required", [])
        assert "section" in required
        assert "key" in required
        assert "value" in required

    def test_update_config_has_description(self, tools):
        tool = next(t for t in tools if t.name == "update_config")
        assert tool.description
        assert len(tool.description) > 10
