"""Shared helper functions for MCP tool handlers.

Extracted from tools.py for modularity (Issue #7).
"""

import asyncio
import json
import logging
import time
from collections.abc import Coroutine
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mcp.types import TextContent

from config.settings import get_settings
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
    *,
    operation: str | None = None,
) -> Any:
    """Resolve and validate context access.

    Args:
        db: Database session
        user_id: User ID
        context_id: Context UUID
        operation: MAE operation vocabulary value for #1286 deny capture
            (threaded by the memory write tools: remember / update / forget).
            ``None`` for callers outside that vocabulary — their binding
            denies stay log-only here.

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
        # %-args (not extra=) so the fields actually render under this module's
        # stdlib logger / "%(message)s" formatter — matches the _log_tool_usage
        # style below; extra={...} would be silently dropped.
        logger.warning(
            "context_write_denied: reason=key_workspace_mismatch context_id=%s "
            "context_workspace_id=%s key_workspace_id=%s user_id=%s",
            str(context_id),
            str(context.workspace_id),
            str(key_workspace_id),
            user_id,
        )
        raise _ContextNotFoundError(context_id, "Context not found or you don't have access to it.")

    # Issue #1275 (RFC-0002 P0-2): subtractive agent-binding WRITE gate for the
    # MCP write tools (remember / update_memory / forget resolve via this
    # helper). The read path inherits the same filter from
    # resolve_context_for_workspace_read. Applied strictly last, so it can
    # only remove access; no-op unless the request authenticated with an
    # agent-bound key. Deny is the same uniform context_not_found shape.
    from auth.agent_scope import get_agent_scope

    scope = get_agent_scope()
    if scope is not None:
        from services.agent_binding_service import ACCESS_WRITE, AgentBindingService

        # #1286 (P0-5): deny capture. In shadow mode the request proceeds
        # into the service-layer gates, which may re-evaluate the same
        # context — the writer's request-scoped dedup collapses those shadow
        # rows to one, so this pre-gate emits unconditionally. A hard deny
        # stops the request HERE (the service gate is never reached), so its
        # emission is load-bearing.
        allowed, decision = await AgentBindingService(db).evaluate_context_access(
            scope,
            context_id,
            ACCESS_WRITE,
            operation=operation,
            user_id=user_id,
        )
        if not allowed:
            logger.warning(
                "context_write_denied: reason=agent_binding decision=%s context_id=%s "
                "agent_id=%s user_id=%s",
                decision,
                str(context_id),
                str(scope.agent_id),
                user_id,
            )
            raise _ContextNotFoundError(
                context_id, "Context not found or you don't have access to it."
            )

    return context


async def _resolve_context_for_read(
    db: "AsyncSession",
    user_id: str,
    context_id: UUID,
    *,
    required_role: str = "viewer",
    operation: str | None = None,
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
        # #1286 (P0-5): ``operation`` threads MAE audit identity into the
        # binding filter so an enforce-mode hard deny at THIS pre-gate — which
        # stops the request before any service-layer gate — still persists
        # its denied row (the MCP read face of the #1291/#1292 parity lesson).
        return await PermissionService(db).resolve_context_for_workspace_read(
            user_id=user_id,
            context_id=context_id,
            required_role=required_role,
            key_workspace_id=_mcp_key_workspace_scope.get(),
            operation=operation,
        )
    except NotFoundException as exc:
        raise _ContextNotFoundError(
            context_id,
            "Context not found or you don't have access to it.",
        ) from exc


async def _touch_context_last_used(db: "AsyncSession", context: Any) -> bool:
    """Throttled touch of ``Context.last_used_at`` (Issue #1257).

    ``last_used_at`` feeds the ``list_contexts`` recency sort but was never
    written after row creation, so it was effectively ``created_at``. Memory
    operations call this on the Context row they already resolved. Same
    hot-row reasoning as the api_keys throttle (#947): the in-memory precheck
    against the loaded row costs no query at all while the stored timestamp is
    fresher than the window.

    CONTRACT — call this immediately before the handler's ``db.commit()``, and
    when touching several contexts in one request, in ascending ``id`` order:

    - The write is a single guarded Core UPDATE, not a dirty ORM attribute.
      A dirty attribute would be autoflushed into the service pipeline (row
      lock held across embedding/Qdrant I/O), persisted by any collaborator
      that commits the shared session mid-request (e.g.
      ``ContextSearchConfigRepository.create_or_get``), and would fire
      ``Context.updated_at``'s ``onupdate=func.now()``. Executing at commit
      time bounds the row lock to one round-trip and keeps error paths clean.
    - ``updated_at`` is pinned to itself in SET so the column-level
      ``onupdate`` does NOT fire — a recall must not rewrite the context's
      "last modified" timestamp shown in the REST API.
    - The WHERE clause re-checks the throttle so two concurrent requests that
      both pass the in-memory precheck race safely (second one matches 0 rows).
    - Ascending-id ordering across multiple touches keeps concurrent
      overlapping cross-context recalls deadlock-free.

    tz note: this column is ``DateTime(timezone=True)`` — one of the few AWARE
    columns (see .claude/rules/backend.md) — so the touch writes an aware UTC
    value; ``list_contexts`` sorts against an aware ``_UTC_MIN`` sentinel. A
    naive stored value (nullable legacy data / direct-SQL writes) is
    normalized to UTC rather than crashing the precheck.

    Args:
        db: The handler's session (the UPDATE rides its next commit).
        context: Resolved ORM ``Context`` (only ``id``/``last_used_at`` read).

    Returns:
        True if the guarded UPDATE was issued, False if throttled.
    """
    now = datetime.now(UTC)
    last = context.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    throttle = timedelta(seconds=get_settings().context_last_used_throttle_seconds)
    if last is not None and now - last < throttle:
        return False

    from sqlalchemy import or_, update

    from models.auth import Context

    await db.execute(
        update(Context)
        .where(Context.id == context.id)
        .where(or_(Context.last_used_at.is_(None), Context.last_used_at <= now - throttle))
        .values(last_used_at=now, updated_at=Context.updated_at)
    )
    return True


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
    *,
    attributed_context_ids: list[UUID] | None = None,
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
        attributed_context_ids: #1228 — ADDITIONAL contexts read by this
            call (cross-context recall lists several but bills one unit).
            Each gets a diagnostic row in context_read_attributions,
            structurally invisible to UsageStats row-count consumers
            (quota, workspace analytics); the memory-health retrieval
            grading merges them into per-context read counts.
    """
    from db.base import get_db
    from utils.usage_logger import log_usage

    response_time_ms = int((time.time() - start_time) * 1000)
    try:
        # Use independent session: log_usage() calls db.commit() internally,
        # which would prematurely commit the handler's transaction if shared.
        async for log_db in get_db():
            billable_row_written = await log_usage(
                db=log_db,
                user_id=user_id,
                endpoint=f"mcp:{tool_name}",
                method="MCP",
                status_code=status_code,
                response_time_ms=response_time_ms,
                context_id=str(context_id) if context_id else None,
                workspace_id=str(workspace_id) if workspace_id else None,
            )
            # #1228: attribution rows only make sense alongside the billable
            # row — log_usage swallows its own failure, and writing the
            # secondaries without the primary would count reads on the
            # listed contexts while the primary's read stays invisible.
            if billable_row_written and attributed_context_ids:
                from models.auth import ContextReadAttribution
                from utils.datetime import utcnow

                now = utcnow()
                for cid in attributed_context_ids:
                    log_db.add(
                        ContextReadAttribution(
                            user_id=user_id,
                            context_id=cid,
                            endpoint=f"mcp:{tool_name}",
                            created_at=now,
                        )
                    )
                await log_db.commit()
    except Exception as e:
        logger.warning("tool_usage_log_failed: tool=%s error=%s", tool_name, str(e))
