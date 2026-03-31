"""Shared helper functions for MCP tool handlers.

Extracted from tools.py for modularity (Issue #7).
"""

import asyncio
import json
import logging
import time
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._constants import T, get_tool_timeout

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ============================================================================
# Timeout
# ============================================================================


async def execute_with_timeout(
    coro: Coroutine[Any, Any, T],
    timeout: float | None = None,
    operation_name: str = "tool",
) -> T:
    """Execute a coroutine with timeout protection.

    Issue #163: Prevents tool execution from hanging indefinitely due to
    downstream service issues (Qdrant, embedding API, reranker).

    Args:
        coro: Coroutine to execute
        timeout: Timeout in seconds (default: uses per-tool timeout or 60s)
        operation_name: Name for logging purposes (also used to look up timeout)

    Returns:
        Result of the coroutine

    Raises:
        TimeoutError: If execution exceeds timeout
    """
    if timeout is None:
        timeout = get_tool_timeout(operation_name)

    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        logger.error(f"Tool execution timeout: operation={operation_name}, timeout={timeout}s")
        raise


# ============================================================================
# Error / Response helpers
# ============================================================================


def _error_response(error: str, message: str, **extra: Any) -> list[TextContent]:
    """Create a standardized error response.

    Args:
        error: Error code
        message: Human-readable error message
        **extra: Additional fields to include in response

    Returns:
        List with single TextContent error response
    """
    return [
        TextContent(
            type="text",
            text=json.dumps({"status": "error", "error": error, "message": message, **extra}),
        )
    ]


def _context_response_fields(context: Any) -> dict[str, Any]:
    """Extract common context fields for tool responses.

    Args:
        context: Context object (or None)

    Returns:
        Dict with context_id, context_name, context_display_name, context_is_private
    """
    if not context:
        return {
            "context_id": None,
            "context_name": None,
            "context_display_name": None,
            "context_is_private": None,
        }
    return {
        "context_id": str(context.id),
        "context_name": context.name,
        "context_display_name": context.display_name,
        "context_is_private": context.is_private,
    }


# ============================================================================
# Context resolution
# ============================================================================


class _ContextNotFoundError(Exception):
    """Internal error for context resolution failures."""

    def __init__(self, context_id: UUID, message: str):
        self.context_id = context_id
        self.message = message
        super().__init__(message)

    def to_response(self) -> list[TextContent]:
        return _error_response(
            "context_not_found",
            self.message,
            context_id=str(self.context_id),
            help="Use list_contexts() to see contexts you have access to.",
        )


async def _resolve_context(
    db: "AsyncSession",
    user_id: str,
    context_id: UUID,
) -> Any:
    """Resolve and validate context access.

    Args:
        db: Database session
        user_id: User ID
        context_id: Context UUID

    Returns:
        Context object

    Raises:
        _ContextNotFoundError: If context not found or access denied
    """
    from services.context_service import ContextService
    from utils.exceptions import NotFoundException

    context_service = ContextService(db)
    try:
        return await context_service.get_context(user_id, context_id)
    except Exception as e:
        if isinstance(e, NotFoundException):
            error_msg = "Context not found or you don't have access to it."
        else:
            error_msg = str(e)
        raise _ContextNotFoundError(context_id, error_msg) from e


def _resolve_context_id(arg_context_id: str) -> UUID:
    """Parse and return context_id from tool argument.

    Args:
        arg_context_id: Context ID from tool argument (required)

    Returns:
        Parsed UUID

    Raises:
        ValueError: If arg_context_id is invalid UUID format
    """
    try:
        return UUID(arg_context_id)
    except (ValueError, AttributeError, TypeError) as e:
        raise ValueError(
            f"Invalid context_id format: '{arg_context_id}'. "
            f"Expected a UUID (example: 'b3abeabe-7ab1-44bd-8e52-18a191bda66b'). "
            f"Use list_contexts() to discover valid context IDs."
        ) from e


# ============================================================================
# Validation
# ============================================================================


def _validate_memory_id(
    args: dict[str, Any], tool_name: str
) -> tuple[UUID | None, list[TextContent] | None]:
    """Validate and parse memory_id from args.

    Args:
        args: Tool arguments
        tool_name: Tool name for error messages

    Returns:
        (memory_uuid, None) on success, (None, error_response) on failure
    """
    if "memory_id" not in args:
        return None, _error_response(
            "memory_id_required",
            f"{tool_name} requires memory_id argument.",
            help="Get memory_id from recall() results first.",
        )
    try:
        return UUID(args["memory_id"]), None
    except (ValueError, AttributeError, TypeError):
        return None, _error_response(
            "invalid_memory_id_format",
            f"Invalid memory_id format: '{args['memory_id']}'. Expected a UUID.",
            help="Use recall() to get valid memory IDs.",
        )


# ============================================================================
# Permission checks
# ============================================================================


async def _get_workspace_member_role(
    db: "AsyncSession", user_id: str, workspace_id: UUID
) -> str | None:
    """Get user's role in workspace.

    Args:
        db: Database session
        user_id: User ID
        workspace_id: Workspace ID

    Returns:
        Role string ('owner', 'admin', 'member', 'viewer') or None if not a member
    """
    from sqlalchemy import select

    from models.auth import WorkspaceMember

    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user_id, WorkspaceMember.workspace_id == workspace_id
        )
    )
    member = result.scalar_one_or_none()
    return member.role if member else None


async def _check_viewer_permission(
    db: "AsyncSession",
    user_id: str,
    workspace_id: UUID | None,
    operation: str,
) -> list[TextContent] | None:
    """Check if user is a viewer (read-only). Returns error response if so, None otherwise.

    Args:
        db: Database session
        user_id: User ID
        workspace_id: Workspace ID (None = skip check)
        operation: Operation description for error message

    Returns:
        Error response if viewer, None if allowed
    """
    if not workspace_id:
        return None

    user_role = await _get_workspace_member_role(db, user_id, workspace_id)
    if user_role == "viewer":
        return _error_response(
            "permission_denied",
            f"Viewers have read-only access. Cannot {operation}.",
            your_role="viewer",
            required_role="member",
            help="Contact your workspace owner to upgrade your role to 'member' for write access.",
        )
    return None


# ============================================================================
# Usage logging
# ============================================================================


async def _log_tool_usage(
    db: "AsyncSession",
    user_id: str,
    tool_name: str,
    start_time: float,
    status_code: int,
    context_id: UUID | str | None = None,
    workspace_id: UUID | None = None,
) -> None:
    """Log tool usage metrics.

    Args:
        db: Database session
        user_id: User ID
        tool_name: Tool name
        start_time: Start time from time.time()
        status_code: HTTP-style status code (200=success, 500=error)
        context_id: Context ID (optional)
        workspace_id: Workspace ID (optional)
    """
    from db.base import get_db
    from utils.usage_logger import log_usage

    response_time_ms = int((time.time() - start_time) * 1000)
    try:
        async for log_db in get_db():
            await log_usage(
                db=log_db,
                user_id=user_id,
                endpoint=f"mcp:{tool_name}",
                method="MCP",
                status_code=status_code,
                response_time_ms=response_time_ms,
                context_id=str(context_id) if context_id else None,
                workspace_id=str(workspace_id) if workspace_id else None,
            )
    except Exception as e:
        logger.warning("tool_usage_log_failed: tool=%s error=%s", tool_name, str(e))
