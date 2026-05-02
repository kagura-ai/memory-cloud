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
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._arg_coercion import coerce_mcp_arguments
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
        "get_usage",
        "get_sleep_report",  # Uses report_id, not context_id
        "rollback_sleep_run",  # Uses report_id, not context_id
        "setup_resource",  # Uses resource_id, not context_id
        "ingest_events",  # Uses resource_id, not context_id
        "get_resource_impact",  # Uses resource_id, not context_id
        "get_resource_schema",  # Uses resource_id, not context_id
        "list_resource_tokens",  # Uses resource_id, not context_id
        "get_analysis",  # Issue #496: uses run_id, not context_id
        "get_cluster",  # Issue #496: uses run_id, not context_id
    }
)

# Read-only info tools exempt from rate limiting
_RATE_LIMIT_EXEMPT_TOOLS = frozenset(
    {
        "get_usage",
        "list_contexts",
        "get_context_info",  # Must remain callable so agents can inspect context even when rate-limited
        "list_edges",  # Read-only edge listing
        "get_sleep_history",  # Read-only sleep report listing
        "get_sleep_report",  # Read-only sleep report detail
        "get_resource_impact",  # Read-only resource stats
        "get_resource_schema",  # Read-only schema fetch
        "list_resource_tokens",  # Read-only token listing
        "get_analysis",  # Issue #496: read-only analysis row fetch
        "list_analyses",  # Issue #496: read-only analysis list
        "get_active_analysis",  # Issue #496: read-only most-recent succeeded
        "get_cluster",  # Issue #496: read-only cluster drill-down
    }
)

# Per-workspace rate limit cache {workspace_id: (allowed, used, limit, expires_at)}
_RATE_LIMIT_CACHE: dict[UUID, tuple[bool, int, int, float]] = {}
_RATE_LIMIT_CACHE_TTL = 60  # seconds
_RATE_LIMIT_CACHE_MAX_SIZE = 1000

# Lazy-initialized registry (avoids circular imports at module load time)
_TOOL_REGISTRY: dict[str, Any] | None = None


def _build_registry() -> dict[str, Any]:
    """Build tool name → handler mapping."""
    from mcp_server.tools.analysis import (
        handle_analyze_context,
        handle_get_active_analysis,
        handle_get_analysis,
        handle_get_cluster,
        handle_list_analyses,
    )
    from mcp_server.tools.context import (
        handle_create_context,
        handle_delete_context,
        handle_get_context_info,
        handle_list_contexts,
        handle_merge_contexts,
        handle_update_context,
    )
    from mcp_server.tools.edge import (
        handle_create_edge,
        handle_delete_edge,
        handle_list_edges,
        handle_update_edge,
    )
    from mcp_server.tools.resource import (
        handle_get_resource_impact,
        handle_get_resource_schema,
        handle_ingest_events,
        handle_list_resource_tokens,
        handle_setup_resource,
    )
    from mcp_server.tools.search_config import handle_update_search_config
    from mcp_server.tools.sleep import (
        handle_get_sleep_history,
        handle_get_sleep_report,
        handle_rollback_sleep_run,
    )
    from mcp_server.tools.usage import handle_get_usage

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
        "list_edges": handle_list_edges,
        "create_edge": handle_create_edge,
        "update_edge": handle_update_edge,
        "delete_edge": handle_delete_edge,
        "get_usage": handle_get_usage,
        "get_sleep_history": handle_get_sleep_history,
        "get_sleep_report": handle_get_sleep_report,
        "rollback_sleep_run": handle_rollback_sleep_run,
        "setup_resource": handle_setup_resource,
        "ingest_events": handle_ingest_events,
        "get_resource_impact": handle_get_resource_impact,
        "get_resource_schema": handle_get_resource_schema,
        "list_resource_tokens": handle_list_resource_tokens,
        # Issue #496: Memory Broadlistening — 5 tools sharing the same
        # 4-stage gate chain as REST /api/v1/contexts/{id}/analyses.
        "analyze_context": handle_analyze_context,
        "get_analysis": handle_get_analysis,
        "list_analyses": handle_list_analyses,
        "get_active_analysis": handle_get_active_analysis,
        "get_cluster": handle_get_cluster,
    }


async def _check_rate_limit(workspace_id: UUID) -> tuple[bool, int, int]:
    """Check MCP rate limit with TTL cache and in-memory counter.

    On cache miss: queries DB for today's count and caches (used, limit, expires_at).
    On cache hit: increments in-memory used counter to track calls within the TTL window,
    preventing limit overshoot between DB refreshes.

    Args:
        workspace_id: Workspace ID

    Returns:
        Tuple of (allowed, used_today, daily_limit)
    """
    now = time.monotonic()
    cached = _RATE_LIMIT_CACHE.get(workspace_id)
    if cached is not None:
        allowed, used, limit, expires_at = cached
        if now < expires_at:
            # Increment in-memory counter to prevent overshoot within TTL
            used += 1
            new_allowed = used < limit
            _RATE_LIMIT_CACHE[workspace_id] = (new_allowed, used, limit, expires_at)
            return new_allowed, used, limit

    from db.base import get_db
    from services.quota_service import QuotaService

    async for db in get_db():
        allowed, used, limit = await QuotaService(db).check_mcp_rate_limit(workspace_id)
        # Evict expired entries when cache is full
        if len(_RATE_LIMIT_CACHE) >= _RATE_LIMIT_CACHE_MAX_SIZE:
            expired = [k for k, v in _RATE_LIMIT_CACHE.items() if v[3] <= now]
            for k in expired:
                del _RATE_LIMIT_CACHE[k]
            if len(_RATE_LIMIT_CACHE) >= _RATE_LIMIT_CACHE_MAX_SIZE:
                oldest_key = min(_RATE_LIMIT_CACHE, key=lambda wid: _RATE_LIMIT_CACHE[wid][3])
                del _RATE_LIMIT_CACHE[oldest_key]
        _RATE_LIMIT_CACHE[workspace_id] = (allowed, used, limit, now + _RATE_LIMIT_CACHE_TTL)
        return allowed, used, limit

    logger.warning("rate_limit_db_unavailable: allowing request as fallback")
    return True, 0, 0


def invalidate_rate_limit_cache(workspace_id: UUID | None = None) -> None:
    """Invalidate rate limit cache. For testing and admin use.

    Args:
        workspace_id: Specific workspace to invalidate, or None for all
    """
    if workspace_id is None:
        _RATE_LIMIT_CACHE.clear()
    else:
        _RATE_LIMIT_CACHE.pop(workspace_id, None)


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
    Issue #149: Rate limit check before dispatch.

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

    # Validate tool exists before expensive checks (avoids DB query for unknown tools)
    handler = _TOOL_REGISTRY.get(tool_name)
    if handler is None:
        return _error_response("unknown_tool", f"Unknown tool: {tool_name}")

    # Issue #196 / #197: some MCP clients serialize arrays / objects / booleans
    # as JSON strings. Coerce them back to their declared types before the
    # handler constructs its pydantic request model.
    args = coerce_mcp_arguments(tool_name, args)

    # Rate limit check (exempt read-only info tools)
    if workspace_id and tool_name not in _RATE_LIMIT_EXEMPT_TOOLS:
        try:
            allowed, used, limit = await _check_rate_limit(workspace_id)
            if not allowed:
                return _error_response(
                    "rate_limit_exceeded",
                    f"Daily MCP call limit reached ({used}/{limit}). Resets at midnight UTC.",
                    used_today=used,
                    daily_limit=limit,
                    help="Use get_usage() to check your current quota. "
                    "Upgrade your plan for higher limits.",
                )
        except Exception as e:
            # Don't block tool execution if rate limit check fails
            logger.warning(f"rate_limit_check_failed: {e}")

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
