"""Shared helper functions for MCP tool handlers.

Extracted from tools.py for modularity (Issue #7).
"""

import asyncio
import json
import logging
import time
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._constants import T, get_tool_timeout

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ============================================================================
# Issue #963: per-request API-key workspace scope (MCP confinement)
# ============================================================================
# The MCP transport conflates ``workspace_id`` (api_key_workspace_id for a
# workspace-scoped key, else the user's *current* workspace for OAuth2/session/
# global-key auth — see transport.py). For workspace confinement we need the
# PURE key scope: the API key's own workspace, or None when the request is not
# authenticated with a workspace-scoped key. The transport sets this contextvar
# once per request right after auth (the single authenticate_mcp_request call),
# and the context-resolution chokepoints below read it — so confinement applies
# to every MCP read AND write tool without threading a param through ~25 handler
# signatures, and never over-confines a session/OAuth/global-key caller.
_mcp_key_workspace_scope: ContextVar["UUID | None"] = ContextVar(
    "mcp_key_workspace_scope", default=None
)


def set_mcp_key_workspace_scope(workspace_id: "UUID | None") -> None:
    """Set the per-request API-key workspace scope (None unless the request is
    authenticated with a workspace-scoped API key). Called by the MCP transport
    after authentication; read by ``_resolve_context_for_read`` / ``_resolve_context``."""
    _mcp_key_workspace_scope.set(workspace_id)


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


def _success_response(**data: Any) -> list[TextContent]:
    """Create a standardized success response.

    Args:
        **data: Fields to include in the response

    Returns:
        List with single TextContent success response
    """
    return [
        TextContent(
            type="text",
            text=json.dumps({"status": "success", **data}),
        )
    ]


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
            "context_is_locked": None,
        }
    return {
        "context_id": str(context.id),
        "context_name": context.name,
        "context_display_name": context.display_name,
        "context_is_private": context.is_private,
        "context_is_locked": context.is_locked,
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
        context = await context_service.get_context(user_id, context_id)
    except Exception as e:
        if isinstance(e, NotFoundException):
            error_msg = "Context not found or you don't have access to it."
        else:
            error_msg = str(e)
        raise _ContextNotFoundError(context_id, error_msg) from e

    # Issue #963: confine a workspace-scoped API key to its own workspace on the
    # WRITE path too (handle_remember / update_memory / forget resolve via this
    # helper). ContextService.get_context authorizes on membership in the
    # context's owning workspace — necessary but not sufficient for a
    # workspace-scoped key. Enforce the pure key scope (None unless the request
    # used such a key) with the same uniform _ContextNotFoundError as the read path.
    key_workspace_id = _mcp_key_workspace_scope.get()
    if key_workspace_id is not None and context.workspace_id != key_workspace_id:
        logger.warning(
            "context_write_denied",
            extra={
                "reason": "key_workspace_mismatch",
                "context_id": str(context_id),
                "context_workspace_id": str(context.workspace_id),
                "key_workspace_id": str(key_workspace_id),
                "user_id": user_id,
            },
        )
        raise _ContextNotFoundError(context_id, "Context not found or you don't have access to it.")

    return context


async def _resolve_context_for_read(
    db: "AsyncSession",
    user_id: str,
    context_id: UUID,
    *,
    required_role: str = "viewer",
) -> Any:
    """Resolve a context_id to a Context the caller can read, MCP-flavored.

    Thin wrapper around ``PermissionService.resolve_context_for_workspace_read``
    that translates the domain-flavored ``NotFoundException`` into the
    MCP-native ``_ContextNotFoundError`` so every MCP read tool surfaces the
    same uniform context_not_found shape (CWE-639 / OWASP A01 uniform
    disclosure) regardless of the underlying deny reason (not found, private
    non-creator, not a workspace member, member-suspended, whitelist miss).

    The ``required_role="viewer"`` default matches the HTTP ``/graph/*`` and
    ``/memory/stats`` reference implementations — writers should pass ``admin``
    or ``owner``.

    Issue #963: forwards the per-request API-key workspace scope
    (``set_mcp_key_workspace_scope``, the PURE key scope — None unless the
    request used a workspace-scoped API key) to the service-layer chokepoint as
    ``key_workspace_id``. Reading it from the contextvar (rather than the handler
    ``workspace_id`` param) avoids confining OAuth2/session/global-key callers,
    whose handler ``workspace_id`` is the user's *current* workspace, not a key
    scope. A mismatch raises the same uniform ``_ContextNotFoundError``.
    """
    from services.permission_service import PermissionService
    from utils.exceptions import NotFoundException

    try:
        return await PermissionService(db).resolve_context_for_workspace_read(
            user_id=user_id,
            context_id=context_id,
            required_role=required_role,
            key_workspace_id=_mcp_key_workspace_scope.get(),
        )
    except NotFoundException as exc:
        raise _ContextNotFoundError(
            context_id,
            "Context not found or you don't have access to it.",
        ) from exc


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

    Returns ``None`` when the workspace itself is soft-deleted
    (``Workspace.deleted_at IS NOT NULL``) so MCP write tools do not
    silently authorize ingest into a deleted workspace.

    Args:
        db: Database session
        user_id: User ID
        workspace_id: Workspace ID

    Returns:
        Role string ('owner', 'admin', 'member', 'viewer') or ``None`` if
        not a member or the workspace is soft-deleted.
    """
    from sqlalchemy import select

    from models.auth import Workspace, WorkspaceMember

    result = await db.execute(
        select(WorkspaceMember)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
    )
    member = result.scalar_one_or_none()
    return member.role if member else None


async def _check_workspace_membership(
    db: "AsyncSession",
    user_id: str,
    workspace_id: UUID | None,
    operation: str,
) -> list[TextContent] | None:
    """Membership-only gate for READ-ONLY MCP tools.

    Issue #485: read tools that accept an optional ``workspace_id``
    arg (e.g. ``get_file_download_url``, ``list_files``) need to deny
    callers who are NOT members of the target workspace, but MUST
    allow callers whose role is ``viewer`` — viewers ARE members and
    are entitled to read access by design.

    ``_check_viewer_permission`` (below) explicitly rejects viewers
    as part of write-tool fail-closed gating; using it on read tools
    accidentally blocks viewer reads. This helper is the read-friendly
    sibling: it short-circuits only when membership is missing.

    Args:
        db: Database session.
        user_id: User ID.
        workspace_id: Workspace ID (None = skip check).
        operation: Operation description for error message.

    Returns:
        Error response if the caller is NOT a member of the
        workspace, otherwise ``None``.
    """
    if not workspace_id:
        return None
    user_role = await _get_workspace_member_role(db, user_id, workspace_id)
    if user_role is None:
        return _error_response(
            "permission_denied",
            f"Cannot {operation}: workspace not accessible.",
            your_role="not_a_member",
            required_role="viewer",
            help=(
                "Verify the workspace is active and you are a member "
                "(any role — viewer is sufficient for read access)."
            ),
        )
    return None


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
    if user_role is None:
        # Caller is not a member of this workspace, OR the workspace is
        # soft-deleted (`_get_workspace_member_role` filters Workspace.deleted_at
        # IS NULL). Either way: deny writes (fail-closed).
        return _error_response(
            "permission_denied",
            f"Cannot {operation}: workspace not accessible.",
            your_role="not_a_member",
            required_role="member",
            help="Verify the workspace is active and you are a member with at least 'member' role.",
        )
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
        # Use independent session: log_usage() calls db.commit() internally,
        # which would prematurely commit the handler's transaction if shared.
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
