"""Anonymous read-only public MCP endpoint at ``/mcp/public/{public_id}`` (#2675).

A structurally separate surface: it never authenticates, never calls the
authenticated tool registry or its handlers, and serves five read-only tools
over one published resource context. Everything a public tool reads is built
from ``scope.public_rows``; ``tests/mcp_server/public/test_isolation_guard.py``
keeps the package away from the authenticated surface.
"""
