"""MCP Server factory.

Creates MCP Server instances with registered tools.
"""

import logging
from uuid import UUID

from mcp.server import Server

logger = logging.getLogger(__name__)


def create_mcp_server(
    user_id: str,
    workspace_id: UUID | None = None,  # Workspace ID (Issue #146)
) -> Server:
    """Create MCP Server instance.

    Issue #146: Now accepts workspace_id for workspace-scoped API keys.
    Issue #245: Removed context_id (now required in tool args).
    Issue #248: Removed SSE transport - tools handled by HTTP transport directly.

    Args:
        user_id: User ID for this server instance
        workspace_id: Workspace ID (None for personal)

    Returns:
        MCP Server instance (Streamable HTTP transport uses execute_tool_call directly)

    Example:
        >>> server = create_mcp_server("user_123", workspace_id=UUID("..."))
        >>> # Tools: remember, recall, forget, reference, explore, get_context_info, list_contexts
    """
    server = Server(name=f"kagura-memory-cloud-{user_id}")

    logger.info(f"MCP server created for user={user_id}, workspace={workspace_id}")

    return server
