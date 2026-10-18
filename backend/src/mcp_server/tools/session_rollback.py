"""MCP tool handler: ``rollback_session`` (#2044).

The tool face of ``services.session_rollback``: authorization (context editor;
a session is addressed only with its own principal, a context owner may name
any), the dry-run plan, and the ``rollback_sleep_run``-shaped report —
``partial_rollback`` with per-item causes when an item failed, a refusal once
the session's rollback is final.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._errors import classify_cause, new_correlation_id
from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _context_response_fields,
    _ContextNotFoundError,
    _error_response,
    _log_tool_usage,
    _resolve_context,
    _resolve_context_id,
    _success_response,
    execute_with_timeout,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_SESSION_ID_HELP = (
    "session_id is the session your writes were recorded under: the "
    "gen_ai.conversation.id / session.id OTel baggage of the request, else the "
    "MCP session id (Mcp-Session-Id)."
)
_NOT_FOUND_HELP = (
    "Only your own sessions are visible (the same user and the same agent "
    "credential that wrote them); a context owner may name any session in the "
    "context. changes_since(context_id, since, session_id) lists what a session wrote."
)
_PARTIAL_HELP = (
    "Every item not listed in summary.errors was handled and saved; the failed "
    "items are not retried. Quote the correlation_id when reporting a failure."
)


def _parse_args(args: dict[str, Any]) -> tuple[str, bool] | list[TextContent]:
    from api.correlation import validate_correlation_token

    session_id = args.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return _error_response(
            "missing_fields", "Missing required field: session_id", help=_SESSION_ID_HELP
        )
    if validate_correlation_token(session_id) is None:
        return _error_response(
            "validation_error",
            "session_id must be 1-128 characters of A-Z, a-z, 0-9, '.', '_' or '-'.",
            help=_SESSION_ID_HELP,
        )
    dry_run = args.get("dry_run", True)
    if not isinstance(dry_run, bool):
        return _error_response("validation_error", "dry_run must be a boolean.")
    return session_id, dry_run


async def handle_rollback_session(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Plan or roll back what one session wrote in a context."""
    from auth.workspace_roles import ContextRole
    from db.base import get_db
    from services.session_rollback import (
        AlreadyRolledBackError,
        SessionNotFoundError,
        SessionScope,
        addressable_principal,
        context_role,
        finish,
        rollback_session,
    )

    parsed = _parse_args(args)
    if isinstance(parsed, list):
        return parsed
    session_id, dry_run = parsed
    start = time.time()
    async for db in get_db():
        context_id: UUID | None = None
        try:
            context_id = _resolve_context_id(args["context_id"])
            perm_error = await _check_viewer_permission(
                db, user_id, workspace_id, "roll back sessions"
            )
            if perm_error:
                return perm_error
            # The write path: workspace-key scope and the agent-binding write
            # gate, uniform context_not_found on a deny.
            context = await _resolve_context(db, user_id, context_id)
            role = await context_role(db, user_id, context_id)
            if role not in (ContextRole.OWNER, ContextRole.EDITOR):
                return _error_response(
                    "permission_denied",
                    "Cannot roll back sessions: you need the editor or owner role on this context.",
                    your_role=role.value if role is not None else None,
                    required_role="editor",
                    help="Ask a context owner for the editor role.",
                )
            scope = SessionScope(
                context_id=context_id,
                session_id=session_id,
                principal=addressable_principal(user_id, role),
            )
            result = await execute_with_timeout(
                rollback_session(
                    db,
                    scope=scope,
                    workspace_id=context.workspace_id,
                    requested_by=user_id,
                    dry_run=dry_run,
                    classify=classify_cause,
                    new_correlation_id=new_correlation_id,
                ),
                operation_name="rollback_session",
            )
            failed = bool(result.errors)
            await _log_tool_usage(
                db,
                user_id,
                "rollback_session",
                start,
                500 if failed else 200,
                context_id,
                workspace_id,
            )
            if dry_run:
                await db.rollback()  # a dry run writes nothing
            else:
                await db.commit()
                await finish(result)
        except SessionNotFoundError:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "rollback_session", start, 404, context_id, workspace_id
            )
            return _error_response(
                "session_not_found",
                "No session with this id that you can roll back exists in this context.",
                session_id=session_id,
                help=_NOT_FOUND_HELP,
            )
        except AlreadyRolledBackError as e:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "rollback_session", start, 409, context_id, workspace_id
            )
            return _error_response(
                "already_rolled_back",
                "This session was already rolled back in this context; a rollback runs once.",
                retryable=False,
                session_id=session_id,
                **({"rollback_id": e.rollback_id} if e.rollback_id is not None else {}),
                rollback_status=e.status,
                help="changes_since(context_id, since, session_id) shows what the session wrote.",
            )
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "rollback_session", start, 500, context_id, workspace_id
            )
            raise

        from services.session_rollback import plan_counts

        common: dict[str, Any] = {
            "session_id": session_id,
            "events": result.events,
            "remaining": result.remaining,
            **({"rollback_id": result.rollback_id} if result.rollback_id is not None else {}),
            **_context_response_fields(context),
        }
        items = result.listed_items()
        listing: dict[str, Any] = {"items": items}
        if len(items) < len(result.items):
            listing["items_truncated"] = True
        if dry_run:
            return _success_response(
                status="dry_run",
                dry_run=True,
                plan=plan_counts(result.items),
                **listing,
                **common,
            )
        if failed:
            return _error_response(
                "partial_rollback",
                f"Rollback finished this call with {len(result.errors)} failed item(s); "
                "summary shows what was reversed.",
                help=_PARTIAL_HELP
                + (
                    f" {result.remaining} event(s) remain: call rollback_session again to continue."
                    if result.remaining
                    else ""
                ),
                retryable=False,
                rollback_status=result.status,
                summary=result.summary,
                totals=result.totals,
                **({"correlation_id": result.correlation_id} if result.correlation_id else {}),
                **common,
            )
        return _success_response(
            status="in_progress" if result.remaining else "rolled_back",
            dry_run=False,
            rollback_status=result.status,
            summary=result.summary,
            totals=result.totals,
            **common,
        )

    return _error_response("db_error", "Database unavailable.")
