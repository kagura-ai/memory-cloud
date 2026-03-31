"""MCP tool handler: kagura_memory_usage_guide.

Returns the comprehensive usage guide for Kagura Memory Cloud.
Extracted from tools.py for modularity (Issue #7).
"""

from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._constants import KAGURA_MEMORY_USAGE_GUIDE


async def handle_kagura_memory_usage_guide(
    args: dict[str, Any],
    user_id: str,
    workspace_id: UUID | None,
) -> list[TextContent]:
    """Return comprehensive usage guide for Kagura Memory Cloud."""
    return [TextContent(type="text", text=KAGURA_MEMORY_USAGE_GUIDE)]
