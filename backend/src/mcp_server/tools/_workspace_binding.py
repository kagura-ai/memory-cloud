"""Which workspace an MCP connection works in (#3000, #3001, #3002).

A connection is **bound** to a workspace when one of these holds, in this
order of precedence:

1. the URL names a workspace (``/mcp/w/{id}``) — ``url``;
2. the credential is a workspace-scoped API key — ``key``;
3. the connection has an effective default context that passes the check in
   ``default_context_workspace`` — ``default_context``.

Otherwise it is **unbound** (``unbound``) and its workspace is the user's
current workspace (``users.current_workspace_id``), as before.

The effective default context is the one the tool dispatcher fills a missing
``context_id`` from (``tools._default_context``): ``?context=<uuid>`` on the
URL, else the credential's own default (a user API key's
``default_context_id`` or the OAuth grant's). An invalid ``?context=`` never
falls through to the credential default. The agent-binding fallback of an
agent-bound key is not a binding source: such a key is always workspace-scoped
(``ck_api_keys_agent_requires_workspace``), so precedence 2 binds it first.

The transport decides once per request, before any tool runs, and stores the
outcome here; ``list_contexts`` and ``create_context`` read it to tell a bound
connection from an unbound one. ``users.current_workspace_id`` is never
written by this path.
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Literal
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from mcp_server.tools._default_context import DefaultContextSelection

logger = logging.getLogger(__name__)

WorkspaceBinding = Literal["url", "key", "default_context", "unbound"]

# ``None``: not decided (a handler called outside the transport, which only
# tests do). It reads as bound: the single-workspace behaviour of before.
_mcp_workspace_binding: ContextVar[WorkspaceBinding | None] = ContextVar(
    "mcp_workspace_binding", default=None
)

# Tools that pick their workspace themselves on an unbound connection
# (``list_contexts`` lists every member workspace, ``create_context`` picks
# among the creatable ones): the dispatcher's session-workspace gates (slot,
# capacity) leave them to the handler, which gates the workspace it acts on.
_HANDLER_PICKS_WORKSPACE_TOOLS: frozenset[str] = frozenset({"list_contexts", "create_context"})


def set_mcp_workspace_binding(binding: WorkspaceBinding | None) -> None:
    """Store how this request's workspace was decided (the transport, once per request)."""
    _mcp_workspace_binding.set(binding)


def get_mcp_workspace_binding() -> WorkspaceBinding | None:
    """How this request's workspace was decided; ``None`` = not decided."""
    return _mcp_workspace_binding.get()


def connection_is_unbound() -> bool:
    """True only when the transport found no binding (an undecided request reads as bound)."""
    return _mcp_workspace_binding.get() == "unbound"


def handler_picks_workspace(tool_name: str) -> bool:
    """Whether the handler, not the dispatcher, gates the workspace of this call."""
    return tool_name in _HANDLER_PICKS_WORKSPACE_TOOLS and connection_is_unbound()


def effective_default_context(
    selection: DefaultContextSelection | None, credential_default: UUID | None
) -> tuple[UUID | None, str]:
    """``(context_id, source)`` of the connection's default context (no I/O).

    ``source`` is ``url`` / ``credential`` when a context is named,
    ``url_invalid`` for a ``?context=`` that is not a UUID (no fallthrough to
    the credential, as in the dispatcher), ``none`` otherwise.
    """
    if selection is not None and selection.mode == "url" and selection.context_id is not None:
        return selection.context_id, "url"
    if selection is not None and selection.mode != "none":
        return None, "url_invalid"
    if credential_default is not None:
        return credential_default, "credential"
    return None, "none"


async def default_context_workspace(
    db: AsyncSession, user_id: str, context_id: UUID
) -> tuple[UUID | None, str]:
    """``(workspace_id, reason)``: the context's workspace when it binds the connection.

    One indexed read: the context (live), its workspace (not deleted) and the
    caller's membership row there, then the same rules the read resolvers
    apply (``PermissionService.resolve_context_for_workspace_read``): a
    private context only for its creator or an account linked to it (in the
    same statement), and the member's context access list
    (``reaches_context``). ``reason`` is ``ok`` or why the context does not
    bind: ``not_found`` (missing, deleted, or its workspace deleted),
    ``not_a_member``, ``private``, ``not_reachable``, or the workspace
    cannot be used (``workspace_suspended``, ``feature_not_available``: no
    seat), checked last and only then. Raises on a database
    error (the transport answers 503, like its other lookups).
    """
    from sqlalchemy import and_, select

    from auth.workspace_roles import reaches_context
    from models.auth import Context, Workspace, WorkspaceMember
    from services.identity_link_service import owned_by

    row = (
        await db.execute(
            select(
                Workspace,
                WorkspaceMember,
                (Context.is_private.is_(False) | owned_by(Context.created_by, user_id)).label(
                    "readable"
                ),
            )
            .join(Workspace, Workspace.id == Context.workspace_id)
            .outerjoin(
                WorkspaceMember,
                and_(
                    WorkspaceMember.workspace_id == Context.workspace_id,
                    WorkspaceMember.user_id == user_id,
                ),
            )
            .where(
                Context.id == context_id,
                Context.deleted_at.is_(None),
                Workspace.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        return None, "not_found"
    workspace, member, readable = row
    if member is None:
        return None, "not_a_member"
    if not readable:
        return None, "private"
    if not reaches_context(member.role, member.allowed_context_ids, context_id):
        return None, "not_reachable"
    # The gates the resolvers run after membership: a suspended workspace slot
    # (``workspace_suspended``) and a member without a seat on the plan
    # (``feature_not_available``). Read only when every check above passed.
    from mcp_server.tools.workspaces import _refusal

    refusal = await _refusal(db, workspace, member, user_id)
    if refusal is not None:
        return None, refusal
    return workspace.id, "ok"


async def resolve_default_context_binding(
    user_id: str,
    selection: DefaultContextSelection | None,
    credential_default: UUID | None,
) -> UUID | None:
    """The workspace the connection's default context binds it to, else ``None``.

    ``None`` (the caller keeps the current workspace) when there is no default
    context, and when the default fails the check: then one log line with the
    reason code and where the default came from (no ids, no names). Opens a
    database session only when a default context is set. A failed read raises.
    """
    context_id, source = effective_default_context(selection, credential_default)
    if context_id is None:
        if source == "url_invalid":
            logger.info("mcp_workspace_binding_fallback: reason=url_invalid source=url")
        return None

    from db.base import get_db

    async with contextlib.aclosing(get_db()) as sessions:
        async for db in sessions:
            workspace_id, reason = await default_context_workspace(db, user_id, context_id)
            # Release the pooled connection before the request goes on (a
            # read-only transaction).
            await db.commit()
            if workspace_id is None:
                logger.info("mcp_workspace_binding_fallback: reason=%s source=%s", reason, source)
            return workspace_id
    return None  # pragma: no cover - get_db always yields


async def charge_workspace(
    user_id: str,
    connection_workspace_id: UUID | None,
    context_id: UUID | None,
    key_workspace_id: UUID | None,
    *,
    target_workspace_id: UUID | None = None,
) -> UUID | None:
    """The workspace a tool call counts against (#3001): limit, headroom and row.

    A call on a context counts in the context's workspace when the caller is a
    member there, else in the connection's workspace. Under a workspace-scoped
    key it is always the key's workspace (no read). One indexed read, only
    when the call names a context. ``target_workspace_id`` is a workspace the
    call names instead (``create_context(workspace_id=)``): it counts there
    when the caller is a member. Only a live context in a live workspace
    counts in its workspace. A failed read falls back to the connection's
    workspace (logged): metering never fails a call.
    """
    if key_workspace_id is not None:
        return key_workspace_id
    if context_id is None and target_workspace_id is None:
        return connection_workspace_id
    from sqlalchemy import and_, select

    from db.base import get_db
    from models.auth import Context, Workspace, WorkspaceMember

    if context_id is not None:
        query = (
            select(Workspace.id)
            .join(Context, Context.workspace_id == Workspace.id)
            .where(Context.id == context_id, Context.deleted_at.is_(None))
        )
    else:
        query = select(Workspace.id).where(Workspace.id == target_workspace_id)
    query = query.join(
        WorkspaceMember,
        and_(WorkspaceMember.workspace_id == Workspace.id, WorkspaceMember.user_id == user_id),
    ).where(Workspace.deleted_at.is_(None))

    try:
        async with contextlib.aclosing(get_db()) as sessions:
            async for db in sessions:
                workspace_id = (await db.execute(query)).scalar_one_or_none()
                await db.commit()
                return workspace_id if workspace_id is not None else connection_workspace_id
    except Exception as e:  # noqa: BLE001 - metering falls back, never fails the call
        logger.warning("mcp_charge_workspace_failed: reason=%s", type(e).__name__)
    return connection_workspace_id
