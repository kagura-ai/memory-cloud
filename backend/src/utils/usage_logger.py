"""Common Usage Logger for API and MCP requests.

Provides unified logging for both HTTP API and MCP tool calls.
Issue #48 - Usage Statistics - Plan Limits & Usage Tracking
"""

from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, UsageStats, WorkspaceMember
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


def _as_uuid(value: str | None) -> UUID | None:
    """``value`` as a UUID, ``None`` when it spells none."""
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


async def log_usage(
    db: AsyncSession,
    user_id: str,
    endpoint: str,
    method: str = "POST",
    status_code: int = 200,
    response_time_ms: int | None = None,
    context_id: str | None = None,  # Bugfix: Add context_id for stats tracking
    workspace_id: str | None = None,  # Bugfix: Add workspace_id for stats tracking
    api_key_id: int | None = None,  # Issue #626: per-key attribution
    *,
    attribute_to_context_workspace: bool = False,
) -> bool:
    """Log API or MCP tool usage to usage_stats table.

    This function is used by both:
    - HTTP API (via RequestLoggingMiddleware)
    - MCP tools (via tool handlers)

    Args:
        db: Database session
        user_id: OAuth2 user ID (sub claim)
        endpoint: API endpoint path or MCP tool name
        method: HTTP method or 'MCP' for MCP tools
        status_code: Response status code (200 for success, 500 for error)
        response_time_ms: Response time in milliseconds (optional)
        context_id: Context UUID (optional, for context-specific stats)
        workspace_id: Workspace UUID (optional, for workspace-specific stats)
        api_key_id: API key integer ID (Issue #626). Set on public endpoint
            access attributed to a public-bound key. Soft reference — survives
            key deletion.
        attribute_to_context_workspace: #3001 (MCP tools). The row goes to
            ``context_id``'s workspace when ``user_id`` is a member of it, else
            to ``workspace_id``: a call on a context of another of the user's
            workspaces counts there, not on the connection's. Resolved in the
            INSERT itself (a scalar subquery), so it costs no extra round trip.

    Example usage:
        # HTTP API (from middleware)
        await log_usage(db, user_id, "/api/v1/memory/recall", "GET", 200, 150)

        # MCP tool (from handler)
        await log_usage(db, user_id, "mcp:recall", "MCP", 200, 80)

    Returns:
        True when the row was written WITH the requested attribution; False
        when the insert failed outright OR the row was preserved only via the
        #1318 NULL-context fallback (errors are logged and swallowed —
        logging must never break the main flow). Callers chaining
        attribution-dependent writes (#1228 ContextReadAttribution rows) key
        on this: a degraded row must NOT get secondary attributions, or the
        primary context's read becomes invisible while secondaries are
        attributed.

    Note:
        The FK fallback rolls back the session before retrying. Callers pass
        a dedicated session today (middleware and _log_tool_usage both use a
        fresh get_db()); do NOT call this on a shared session holding
        pending business writes.
    """
    # Use utcnow() for timezone-naive datetime (matches DB schema)
    now = utcnow()

    async def _insert(ctx_id: str | None) -> None:
        workspace_value: Any = workspace_id
        context_uuid = _as_uuid(ctx_id) if attribute_to_context_workspace else None
        if context_uuid is not None:
            workspace_value = func.coalesce(
                select(Context.workspace_id)
                .join(
                    WorkspaceMember,
                    and_(
                        WorkspaceMember.workspace_id == Context.workspace_id,
                        WorkspaceMember.user_id == user_id,
                    ),
                )
                .where(Context.id == context_uuid)
                .scalar_subquery(),
                _as_uuid(workspace_id),
            )
        await db.execute(
            insert(UsageStats).values(
                user_id=user_id,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                response_time_ms=response_time_ms,
                created_at=now,
                date=now.date(),
                context_id=ctx_id,  # Bugfix: Add context_id
                workspace_id=workspace_value,  # Bugfix: Add workspace_id
                api_key_id=api_key_id,  # Issue #626
            )
        )
        await db.commit()

    try:
        await _insert(context_id)
    except IntegrityError as e:
        # #1318: callers may stamp an unvalidated context_id (e.g. a
        # nonexistent UUID straight from the request), violating the
        # usage_stats→contexts FK. Preserve the usage/quota event without
        # the context attribution instead of dropping the row.
        await db.rollback()
        if context_id is None:
            logger.error(
                "usage_logging_failed",
                error=str(e),
                user_id=user_id,
                endpoint=endpoint,
            )
            return False
        try:
            await _insert(None)
        except Exception as retry_err:
            await db.rollback()
            logger.error(
                "usage_logging_failed",
                error=str(retry_err),
                original_error=str(e),
                user_id=user_id,
                endpoint=endpoint,
            )
            return False
        logger.warning(
            "usage_logging_context_fk_fallback",
            context_id=context_id,
            user_id=user_id,
            endpoint=endpoint,
        )
        # Row preserved but WITHOUT the requested context attribution —
        # report False so #1228 attribution writes are skipped (see Returns).
        return False
    except Exception as e:
        await db.rollback()
        logger.error(
            "usage_logging_failed",
            error=str(e),
            user_id=user_id,
            endpoint=endpoint,
        )
        # Don't raise - logging should not break the main flow
        return False

    logger.debug(
        "usage_logged",
        user_id=user_id,
        endpoint=endpoint,
        method=method,
        status=status_code,
        response_time_ms=response_time_ms,
    )
    return True
