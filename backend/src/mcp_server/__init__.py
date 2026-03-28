"""MCP (Model Context Protocol) Remote Server.

Implements MCP over Streamable HTTP transport (spec 2025-03-26) for remote client connections.
Issue #248: SSE transport removed (deprecated in MCP spec 2025-03-26).

Components:
    - server: MCP Server factory (create_mcp_server)
    - session: Session manager (MCPSessionManager)
    - transport: Streamable HTTP transport ASGI app (mcp_asgi_app)
    - auth: Authentication (authenticate_mcp_request)
    - tools: MCP tool definitions (HTTP transport only)
"""

from .auth import authenticate_mcp_request
from .mcp_server import create_mcp_server
from .session import MCPSession, MCPSessionManager
from .transport import mcp_asgi_app

__all__ = [
    "create_mcp_server",
    "MCPSession",
    "MCPSessionManager",
    "mcp_asgi_app",
    "authenticate_mcp_request",
]
