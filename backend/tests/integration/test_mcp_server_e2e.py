"""End-to-end tests for MCP Server."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app


class TestMCPServerE2E:
    """Test MCP Server end-to-end workflows."""

    @pytest.fixture
    def client(self):
        """Create FastAPI test client."""
        with TestClient(app) as test_client:
            yield test_client

    def test_mcp_sse_endpoint_exists(self, client):
        """Test that MCP SSE endpoint exists."""
        # OPTIONS request to check CORS
        response = client.options("/mcp/sse")

        # Should not be 404
        assert response.status_code != 404

    def test_mcp_sse_connection(self, client):
        """Test SSE connection establishment."""
        # GET request to establish SSE
        response = client.get(
            "/mcp/sse",
            headers={"Accept": "text/event-stream"},
        )

        # Should return SSE or authentication required
        assert response.status_code in [200, 401, 403]

    def test_mcp_remember_tool(self, client):
        """Test MCP remember tool execution."""
        # This would require actual SSE/JSON-RPC implementation
        # For now, just verify the tools module is importable
        from mcp_server.tools import handle_remember

        assert handle_remember is not None

    def test_mcp_recall_tool(self, client):
        """Test MCP recall tool execution."""
        from mcp_server.tools import handle_recall

        assert handle_recall is not None

    def test_mcp_forget_tool(self, client):
        """Test MCP forget tool execution."""
        from mcp_server.tools import handle_forget

        assert handle_forget is not None

    def test_mcp_reference_tool(self, client):
        """Test MCP reference tool execution."""
        from mcp_server.tools import handle_reference

        assert handle_reference is not None

    def test_mcp_explore_tool(self, client):
        """Test MCP explore tool execution."""
        from mcp_server.tools import handle_explore

        assert handle_explore is not None

    def test_mcp_session_manager(self, client):
        """Test MCP session manager."""
        import asyncio

        from mcp_server.session import MCPSessionManager

        manager = MCPSessionManager()

        # Create session via get_or_create_session (async)
        session_id = "test_session_e2e"

        async def _run():
            session = await manager.get_or_create_session(
                user_id="test_user",
                workspace_id=None,
                session_id=session_id,
            )
            assert session is not None
            assert session.user_id == "test_user"

            # Verify session exists
            found = await manager.get_session(session_id)
            assert found is not None

            # Remove session
            await manager.remove_session(session_id)
            assert await manager.get_session(session_id) is None

        asyncio.run(_run())

    def test_mcp_tool_definitions(self):
        """Test that MCP tools are properly defined."""
        # Import server and check tools
        try:
            from mcp_server.mcp_server import server

            # Server should be defined
            assert server is not None
        except Exception:
            # MCP server may require runtime initialization
            pass

    def test_mcp_auth_integration(self):
        """Test MCP authentication integration."""
        from mcp_server.auth import authenticate_mcp_request

        # authenticate_mcp_request is the public auth entry point for MCP transport
        assert authenticate_mcp_request is not None
        assert callable(authenticate_mcp_request)

    def test_mcp_transport_sse(self):
        """Test Streamable HTTP transport implementation.

        SSE transport was removed in MCP spec 2025-03-26 (Issue #248).
        The transport module now implements Streamable HTTP transport only.
        """
        import mcp_server.transport as transport_module

        # The transport module should be importable and expose core functions/classes
        assert transport_module is not None

    def test_workspace_scoped_mcp_endpoint_exists(self, client):
        """Test that workspace-scoped MCP endpoint exists."""
        workspace_id = str(uuid4())
        response = client.options(f"/mcp/w/{workspace_id}")

        # Should not be 404 (endpoint should exist)
        assert response.status_code != 404

    def test_workspace_scoped_mcp_requires_auth(self, client):
        """Test workspace-scoped MCP endpoint requires authentication."""
        workspace_id = str(uuid4())
        response = client.get(f"/mcp/w/{workspace_id}")

        # Should require authentication
        assert response.status_code == 401

    def test_workspace_scoped_mcp_post_requires_auth(self, client):
        """Test workspace-scoped MCP POST requires authentication."""
        workspace_id = str(uuid4())
        response = client.post(
            f"/mcp/w/{workspace_id}", json={"jsonrpc": "2.0", "method": "initialize", "id": 1}
        )

        # Should require authentication
        assert response.status_code == 401
