"""MCP Tool registry and execution for Kagura Memory Cloud.

Issue #7: Split monolithic tools.py into focused modules with dict-based registry.

Public API (used by transport.py):
- execute_tool_call(tool_name, arguments, user_id, workspace_id)
- get_tool_definitions()

Handler re-exports (used by test_mcp_server_e2e.py):
- handle_remember, handle_recall, handle_forget, handle_reference, handle_explore
"""

import json
import logging
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._definitions import get_tool_definitions  # noqa: F401
from mcp_server.tools._helpers import _error_response, _resolve_context_id

logger = logging.getLogger(__name__)

# Tools that do NOT require context_id in arguments
_TOOLS_WITHOUT_CONTEXT_ID = frozenset(
    {
        "list_contexts",
        "create_context",
        "update_context",
        "merge_contexts",
        "kagura_memory_usage_guide",
    }
)

# Lazy-initialized registry (avoids circular imports at module load time)
_TOOL_REGISTRY: dict[str, Any] | None = None


def _build_registry() -> dict[str, Any]:
    """Build tool name → handler mapping."""
    from mcp_server.tools.context import (
        handle_create_context,
        handle_delete_context,
        handle_get_context_info,
        handle_list_contexts,
        handle_merge_contexts,
        handle_update_context,
    )
    from mcp_server.tools.guide import handle_kagura_memory_usage_guide
    from mcp_server.tools.search_config import handle_update_search_config

    return {
        "remember": handle_remember,
        "update_memory": handle_update_memory,
        "recall": handle_recall,
        "forget": handle_forget,
        "reference": handle_reference,
        "explore": handle_explore,
        "get_context_info": handle_get_context_info,
        "create_context": handle_create_context,
        "update_context": handle_update_context,
        "delete_context": handle_delete_context,
        "merge_contexts": handle_merge_contexts,
        "list_contexts": handle_list_contexts,
        "update_search_config": handle_update_search_config,
        "kagura_memory_usage_guide": handle_kagura_memory_usage_guide,
    }


async def execute_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    user_id: str,
    workspace_id: UUID | None = None,
) -> list[TextContent]:
    """Execute MCP tool call directly (used by Streamable HTTP Transport).

    Issue #172: Refactored to extract common boilerplate into helpers.
    Issue #245: context_id is now obtained from arguments["context_id"] (required).
    Issue #7: Registry-based dispatch replaces if/elif chain.

    Args:
        tool_name: Tool name (remember, recall, forget, etc.)
        arguments: Tool arguments dict (must include context_id for most tools)
        user_id: User ID for this request
        workspace_id: Workspace ID (Issue #146)

    Returns:
        List of TextContent with execution result
    """
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is None:
        _TOOL_REGISTRY = _build_registry()

    args = arguments or {}

    # Pre-dispatch: validate context_id for tools that require it
    # Issue #81: Skip context_id check if context_ids is provided (cross-context recall)
    if tool_name not in _TOOLS_WITHOUT_CONTEXT_ID:
        has_context_ids = "context_ids" in args and isinstance(args.get("context_ids"), list)
        if "context_id" not in args and not has_context_ids:
            return _error_response(
                "context_id_required",
                f"{tool_name} requires context_id argument.",
                help="Use list_contexts() first to discover available context IDs.",
                example=f'{tool_name}(..., context_id="<uuid-from-list_contexts>")',
            )
        if "context_id" in args:
            try:
                _resolve_context_id(args["context_id"])
            except ValueError as e:
                return _error_response("invalid_context_id_format", str(e))

    # Dispatch to handler
    handler = _TOOL_REGISTRY.get(tool_name)
    if handler is None:
        return _error_response("unknown_tool", f"Unknown tool: {tool_name}")

    try:
        return await handler(args, user_id, workspace_id)
    except Exception as e:
        logger.error(f"mcp_tool_{tool_name}_failed: {e}", exc_info=True)
        return [
            TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )
        ]


# Backward-compat re-exports for test_mcp_server_e2e.py
from mcp_server.tools.explore import handle_explore  # noqa: E402, F401
from mcp_server.tools.memory import (  # noqa: E402, F401
    handle_forget,
    handle_recall,
    handle_reference,
    handle_remember,
    handle_update_memory,
)
