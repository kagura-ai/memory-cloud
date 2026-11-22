"""``list_workspaces`` and the account's other workspaces (#2972).

An MCP connection works in one workspace: the one in its URL
(``/mcp/w/{workspace_id}``) or the account's current one. ``list_workspaces``
names every workspace the account is a member of (a ``WorkspaceMember`` row of
a workspace that is not deleted, the row the transport reads to accept a
``/mcp/w/{id}`` URL), with the account's role there and whether it is the
connection's workspace. ``list_contexts(workspace_id=...)`` then lists one of
them.

A request authenticated with a workspace-scoped API key reaches its key's
workspace only (``get_mcp_key_workspace_scope``): ``list_workspaces`` returns
that workspace alone and ``list_contexts`` names no other.

The tool is in the ``full`` profile, not ``core``; like every tool it stays
callable from any URL (a profile is a view).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._errors import _tool_exception_response
from mcp_server.tools._helpers import (
    _dumps,
    _error_response,
    _log_tool_usage,
    get_mcp_key_workspace_scope,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AccountWorkspace:
    """One workspace of the account: its id, name and the account's role there."""

    id: UUID
    name: str
    role: str


async def account_workspaces(
    db: AsyncSession, user_id: str, *, only: UUID | None = None
) -> list[AccountWorkspace]:
    """The workspaces ``user_id`` is a member of, by name.

    A workspace that is soft-deleted is left out. ``only`` narrows the list to
    one workspace (a workspace-scoped key's).
    """
    from sqlalchemy import select

    from models.auth import Workspace, WorkspaceMember

    query = (
        select(Workspace.id, Workspace.name, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.name, Workspace.id)
    )
    if only is not None:
        query = query.where(Workspace.id == only)
    rows = (await db.execute(query)).all()
    return [AccountWorkspace(id=row[0], name=row[1], role=str(row[2])) for row in rows]


async def other_workspace_count(db: AsyncSession, user_id: str, listed: UUID | None) -> int:
    """How many of the account's workspaces are not ``listed`` (0 under a key scope).

    ``list_contexts`` reports it when the account has more than one workspace,
    so a ``core`` client learns of the others without ``list_workspaces`` in
    its tool list. A workspace-scoped key reaches no other workspace, so it is
    told of none.
    """
    if get_mcp_key_workspace_scope() is not None:
        return 0
    workspaces = await account_workspaces(db, user_id)
    if len(workspaces) < 2:
        return 0
    return sum(1 for workspace in workspaces if workspace.id != listed)


async def handle_list_workspaces(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List the workspaces the account is a member of (#2972).

    Items: ``id``, ``name``, ``role`` (the account's role there) and ``current``
    (the connection's workspace). Takes no arguments.
    """
    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            workspaces = await account_workspaces(db, user_id, only=get_mcp_key_workspace_scope())
            items = [
                {
                    "id": str(workspace.id),
                    "name": workspace.name,
                    "role": workspace.role,
                    "current": workspace.id == workspace_id,
                }
                for workspace in workspaces
            ]
            await _log_tool_usage(
                db, user_id, "list_workspaces", start_time, 200, None, workspace_id
            )
            return [
                TextContent(
                    type="text",
                    text=_dumps({"status": "success", "workspaces": items, "total": len(items)}),
                )
            ]
        except Exception as e:
            await db.rollback()
            return _tool_exception_response("list_workspaces", e)

    return _error_response("internal_error", "Database session unavailable")
