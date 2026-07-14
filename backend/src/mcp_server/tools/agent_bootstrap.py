"""MCP tool: get_agent_bootstrap (RFC-0002 P0-3, Issue #1276).

Session-start composition tool — resolves the agent + its default (or
supplied) context, then composes context guide, pinned memories, a
trusted-only recall (only when a query is supplied), upcoming time memories,
and the agent-state lane into one fail-soft envelope. Pure composition of
existing primitives via ``AgentBootstrapService`` — no parallel retrieval
path (F2 design, ``docs/design/agent-bootstrap-contract.md``).
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _error_response,
    _log_tool_usage,
    _success_response,
    execute_with_timeout,
)


def _parse_uuid(raw: Any) -> UUID | None:
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


async def handle_get_agent_bootstrap(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Rehydrate an agent's cognitive state at session start (owner/agent gated)."""
    if "agent_id" not in args:
        return _error_response("missing_fields", "Missing required field: agent_id")
    agent_id = _parse_uuid(args["agent_id"])
    if agent_id is None:
        return _error_response("invalid_arguments", "agent_id must be a UUID.")

    from auth.agent_scope import get_agent_scope
    from db.base import get_db
    from services.agent_bootstrap_service import (
        AgentBootstrapService,
        BootstrapError,
        BootstrapParams,
        parse_include,
        validate_query,
        validate_session_id,
    )

    # Argument validation (before opening a session) → structured errors.
    try:
        context_id = _parse_uuid(args["context_id"]) if args.get("context_id") else None
        if args.get("context_id") and context_id is None:
            return _error_response("invalid_arguments", "context_id must be a UUID.")
        params = BootstrapParams(
            agent_id=agent_id,
            context_id=context_id,
            session_id=validate_session_id(args.get("session_id")),
            query=validate_query(args.get("query")),
            recall_k=args.get("recall_k"),
            pinned_cap=args.get("pinned_cap"),
            upcoming_until=args.get("upcoming_until"),
            include=parse_include(args.get("include")),
        )
    except BootstrapError as e:
        return _error_response(e.code, e.message)

    # The MCP session carries user_id + workspace; build the principal dict the
    # service reads (agent scope carries the agent-bound identity).
    user = {
        "user_id": user_id,
        "current_workspace_id": workspace_id,
        "api_key_workspace_id": workspace_id,
    }
    agent_scope = get_agent_scope()

    start_time = time.time()
    async for db in get_db():
        try:
            service = AgentBootstrapService(db)
            principal, agent = await service.resolve_principal_and_agent(
                requested_agent_id=agent_id, user=user, agent_scope=agent_scope
            )
            context, binding_info = await service.resolve_context(
                agent=agent, params=params, principal=principal
            )

            # Rate-limit exemption is scoped to query-LESS calls: this tool is in
            # _RATE_LIMIT_EXEMPT_TOOLS (a query-less bootstrap is Postgres-only,
            # like get_context_info / load_pinned). A query-CARRYING bootstrap
            # meters its recall component under the normal MCP rate accounting;
            # on limit that component degrades to rate_limited while the cheap
            # components still return (a session-start tool must stay callable).
            recall_metered = False
            if params.query is not None and workspace_id is not None:
                from mcp_server.tools import _check_rate_limit

                try:
                    allowed, _used, _limit = await _check_rate_limit(workspace_id)
                    recall_metered = not allowed
                except Exception:
                    recall_metered = False

            envelope = await execute_with_timeout(
                service.build_envelope(
                    agent=agent,
                    context=context,
                    binding_info=binding_info,
                    params=params,
                    principal=principal,
                    recall_metered=recall_metered,
                ),
                operation_name="get_agent_bootstrap",
            )

            # #1276: record an operator (owner/admin) "on behalf of" bootstrap
            # so it cannot masquerade as the agent's own activity (no-op for
            # agent-bound calls). Committed atomically with the usage row.
            await service.audit_on_behalf_of(
                agent=agent, principal=principal, session_id=params.session_id
            )
            await _log_tool_usage(
                db, user_id, "get_agent_bootstrap", start_time, 200, context.id, workspace_id
            )
            await db.commit()
            return _success_response(**{k: v for k, v in envelope.items() if k != "status"})

        except BootstrapError as e:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "get_agent_bootstrap", start_time, 404, None, workspace_id
            )
            return _error_response(e.code, e.message)
        except Exception as e:  # pragma: no cover - defensive
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "get_agent_bootstrap", start_time, 500, None, workspace_id
            )
            from mcp_server.tools._helpers import logger

            logger.error(f"get_agent_bootstrap_failed: {e}", exc_info=True)
            return _error_response("internal_error", "Failed to build agent bootstrap.")

    return _error_response("internal_error", "Database session unavailable")
