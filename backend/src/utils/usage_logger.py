"""Common Usage Logger for API and MCP requests.

Provides unified logging for both HTTP API and MCP tool calls.
Issue #48 - Usage Statistics - Plan Limits & Usage Tracking
"""

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import UsageStats
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


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

    Example usage:
        # HTTP API (from middleware)
        await log_usage(db, user_id, "/api/v1/memory/recall", "GET", 200, 150)

        # MCP tool (from handler)
        await log_usage(db, user_id, "mcp:recall", "MCP", 200, 80)

    Returns:
        True when the row was written; False when the insert failed (the
        error is logged and swallowed — logging must never break the main
        flow, but callers chaining dependent writes, e.g. #1228 attribution
        rows, need the signal).
    """
    try:
        # Use utcnow() for timezone-naive datetime (matches DB schema)
        now = utcnow()

        await db.execute(
            insert(UsageStats).values(
                user_id=user_id,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                response_time_ms=response_time_ms,
                created_at=now,
                date=now.date(),
                context_id=context_id,  # Bugfix: Add context_id
                workspace_id=workspace_id,  # Bugfix: Add workspace_id
                api_key_id=api_key_id,  # Issue #626
            )
        )
        await db.commit()

        logger.debug(
            "usage_logged",
            user_id=user_id,
            endpoint=endpoint,
            method=method,
            status=status_code,
            response_time_ms=response_time_ms,
        )
        return True

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
