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
        # Issue #629: binding introspection — list takes no args; describe takes
        # key_id XOR context_id (and its context_id is a *bound* context selector,
        # not a memory-context to resolve), so neither goes through the
        # context_id pre-dispatch.
        "list_my_bindings",
        "describe_binding",
        "list_contexts",
        "create_context",
        "update_context",
        "merge_contexts",
        "get_usage",
        "get_sleep_report",  # Uses report_id, not context_id
        "rollback_sleep_run",  # Uses report_id, not context_id
        "setup_resource",  # Uses resource_id, not context_id
        "setup_connector",  # Uses resource_id, not context_id
        "ingest_events",  # Uses resource_id, not context_id
        "get_resource_impact",  # Uses resource_id, not context_id
        "get_resource_schema",  # Uses resource_id, not context_id
        "list_resource_tokens",  # Uses resource_id, not context_id
        "get_analysis",  # Issue #496: uses run_id, not context_id
        "get_cluster",  # Issue #496: uses run_id, not context_id
        "init_file_upload",  # Issue #485: uses workspace_id + sha256
        "complete_file_upload",  # Issue #485: uses file_id
        "get_file_download_url",  # Issue #485: uses file_id
        "delete_file",  # Issue #485: uses file_id
        "list_files",  # Issue #485: workspace-scoped listing
        # Issue #1128: secret store — workspace-scoped, not memory-context-scoped.
        "secret_register_pubkey",
        "secret_put",
        "secret_get",
        "secret_list",
        "secret_revoke_grant",
    }
)

# Read-only info tools exempt from rate limiting
_RATE_LIMIT_EXEMPT_TOOLS = frozenset(
    {
        "list_my_bindings",  # Issue #629: read-only binding listing
        "describe_binding",  # Issue #629: read-only binding detail
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
        "get_file_download_url",  # Issue #485: read-only presigned GET
        "list_files",  # Issue #485: read-only workspace listing
        "list_tags",  # Issue #614: read-only tag discovery
        "recall_upcoming",  # Issue #877: deterministic read-only Time Memory window query (no Hebbian write)
        "load_pinned",  # Issue #886: deterministic always-load read (must run every turn; no Hebbian write)
        # Issue #1128: secret-store tools carry NO embedding/LLM cost (the memory
        # quota's cost driver) and must stay callable on EVERY plan — an agent has
        # to be able to fetch its deploy key even after heavy recall use. Available
        # on all plans (free/basic/pro); workspace-role gating + the append-only
        # audit log bound abuse, not the memory rate limit.
        "secret_register_pubkey",
        "secret_put",
        "secret_get",
        "secret_list",
        "secret_revoke_grant",
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
    from mcp_server.tools.api_keys import handle_describe_binding, handle_list_my_bindings
    from mcp_server.tools.context import (
        handle_create_context,
        handle_delete_context,
        handle_get_context_info,
        handle_list_contexts,
        handle_list_tags,
        handle_merge_contexts,
        handle_update_context,
    )
    from mcp_server.tools.edge import (
        handle_create_edge,
        handle_delete_edge,
        handle_list_edges,
        handle_update_edge,
    )
    from mcp_server.tools.feedback import handle_feedback
    from mcp_server.tools.files import (
        handle_complete_file_upload,
        handle_delete_file,
        handle_get_file_download_url,
        handle_init_file_upload,
        handle_list_files,
    )
    from mcp_server.tools.resource import (
        handle_get_resource_impact,
        handle_get_resource_schema,
        handle_ingest_events,
        handle_list_resource_tokens,
        handle_setup_connector,
        handle_setup_resource,
    )
    from mcp_server.tools.search_config import handle_update_search_config
    from mcp_server.tools.secrets import (
        handle_secret_get,
        handle_secret_list,
        handle_secret_put,
        handle_secret_register_pubkey,
        handle_secret_revoke_grant,
    )
    from mcp_server.tools.sleep import (
        handle_get_sleep_history,
        handle_get_sleep_report,
        handle_rollback_sleep_run,
    )
    from mcp_server.tools.state import handle_get_state, handle_set_state
    from mcp_server.tools.usage import handle_get_usage

    return {
        # Issue #629: read-only API-key binding introspection
        "list_my_bindings": handle_list_my_bindings,
        "describe_binding": handle_describe_binding,
        "remember": handle_remember,
        "update_memory": handle_update_memory,
        "recall": handle_recall,
        "recall_upcoming": handle_recall_upcoming,
        "load_pinned": handle_load_pinned,
        "forget": handle_forget,
        "reference": handle_reference,
        "explore": handle_explore,
        # Issue #889: agent session-state lane (TTL, recall-excluded)
        "set_state": handle_set_state,
        "get_state": handle_get_state,
        # Issue #888: retrieval feedback signal (append-only, recall-excluded)
        "feedback": handle_feedback,
        "get_context_info": handle_get_context_info,
        "create_context": handle_create_context,
        "update_context": handle_update_context,
        "delete_context": handle_delete_context,
        "merge_contexts": handle_merge_contexts,
        "list_contexts": handle_list_contexts,
        "list_tags": handle_list_tags,  # Issue #614
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
        "setup_connector": handle_setup_connector,
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
        # Issue #485: platform-managed file storage — 5 tools sharing
        # FileStorageService with REST /api/v1/files routes.
        "init_file_upload": handle_init_file_upload,
        "complete_file_upload": handle_complete_file_upload,
        "get_file_download_url": handle_get_file_download_url,
        "delete_file": handle_delete_file,
        "list_files": handle_list_files,
        # Issue #1128: zero-knowledge secret store (workspace-scoped).
        "secret_register_pubkey": handle_secret_register_pubkey,
        "secret_put": handle_secret_put,
        "secret_get": handle_secret_get,
        "secret_list": handle_secret_list,
        "secret_revoke_grant": handle_secret_revoke_grant,
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
    handle_load_pinned,
    handle_recall,
    handle_recall_upcoming,
    handle_reference,
    handle_remember,
    handle_update_memory,
)
