"""MCP Server API Routes.

Provides information about MCP server status and available tools.
Issue #45: Web UI Endpoint Implementation
"""

from fastapi import APIRouter
from pydantic import BaseModel

from auth.dependencies import APIKeyOrSessionUser
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp-api"])


# ============================================================================
# Schemas
# ============================================================================


class MCPToolInfo(BaseModel):
    """MCP Tool information."""

    name: str
    description: str
    input_schema: dict


class MCPToolsResponse(BaseModel):
    """MCP tools list response."""

    tools: list[MCPToolInfo]
    total: int


class MCPStatusResponse(BaseModel):
    """MCP server status response."""

    status: str
    active_sessions: int
    total_tools: int
    tools_available: list[str]
    last_activity: str | None


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/tools", response_model=MCPToolsResponse)
async def list_mcp_tools(
    user: APIKeyOrSessionUser,
):
    """List all available MCP tools.

    Args:
        user: Authenticated user

    Returns:
        List of MCP tools with descriptions and schemas
    """
    from mcp_server.tools._definitions import get_tool_definitions

    tools = [
        MCPToolInfo(
            name=definition["name"],
            description=definition["description"],
            input_schema=definition["inputSchema"],
        )
        for definition in get_tool_definitions()
    ]

    user_id = user.get("user_id", "unknown") if user else "unknown"
    logger.info(f"mcp_tools_listed: user={user_id}, count={len(tools)}")

    return MCPToolsResponse(tools=tools, total=len(tools))


@router.get("/status", response_model=MCPStatusResponse)
async def get_mcp_status(
    user: APIKeyOrSessionUser,
):
    """Get MCP server status.

    Args:
        user: Authenticated user

    Returns:
        MCP server status including active sessions and tools
    """
    # TODO: Implement actual session tracking
    # For now, return static info

    tools_available = [
        "remember",
        "recall",
        "reference",
        "forget",
        "explore",
        "get_stats",
    ]

    user_id = user.get("user_id", "unknown") if user else "unknown"
    logger.info(f"mcp_status_retrieved: user={user_id}")

    return MCPStatusResponse(
        status="running",
        active_sessions=0,  # TODO: Track from session manager
        total_tools=len(tools_available),
        tools_available=tools_available,
        last_activity=None,  # TODO: Track last MCP call
    )
