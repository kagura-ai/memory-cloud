"""The workspace slot gate: a suspended workspace serves nobody (Issue #2918).

When an owner owns more workspaces than their allowance, the excess ones are
suspended (``services.workspace_slots``; the rows are written there and only
there). This module is the read side every surface shares: a suspended
workspace is refused to every member and every credential — Web UI, REST, MCP,
API / agent / connector keys, resource tokens, share keys, public endpoints,
the sync worker — and its background jobs pause, while the owner keeps a short
management path (export, delete, billing handoff, revoking credentials).

Pieces:

- ``SlotPolicy``: ``BLOCKED`` (the default everywhere) or ``OWNER_MANAGEMENT``
  (the workspace's owner passes, everyone else is refused). Every
  ``OWNER_MANAGEMENT`` call site is pinned by
  ``tests/api/test_slot_suspension_route_table.py``.
- ``slot_suspended``: one indexed lookup; the read-time rule of
  ``services.workspace_slots.suspension_in_force`` (a row counts only while its
  stamped owner still owns the live workspace and nobody pays for it).
- ``ensure_slot_active``: raise ``WorkspaceSlotSuspendedError`` (403
  ``SLOT-001``), worded for the owner, a member or an outsider.
- ``active_workspace_predicate``: the SQL twin, for listings and jobs.

The gate runs AFTER authorization (an outsider, or a member outside the
context's reach, keeps the uniform not-found), and it only reads: no row lock,
no advisory lock. Unless ``WORKSPACE_SLOT_SUSPENSION`` is ``enforce`` it runs
no statement at all, and the predicate is ``None`` (callers add nothing), so
the code is deployable before the tables exist.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import ColumnElement, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import Workspace
from services.capacity_lock import is_workspace_member
from services.workspace_slots import (
    suspended_workspace_ids,
    suspension_in_force,
    workspace_slots_enforced,
)
from utils.exceptions import WorkspaceSlotSuspendedError
from utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "MANAGE_PATH",
    "SlotPolicy",
    "active_workspace_predicate",
    "ensure_slot_active",
    "manage_url",
    "slot_suspended",
]

# The account page where the owner chooses which workspaces stay active.
MANAGE_PATH = "/profile/workspaces"


class SlotPolicy(StrEnum):
    """How a gated call treats a slot-suspended workspace (#2918).

    * ``BLOCKED`` — refused for everyone, the owner included (the default).
    * ``OWNER_MANAGEMENT`` — the workspace's owner passes (export, delete, the
      billing handoff, revoking credentials); a member or admin is refused.
      Every use is pinned to an exact set of call sites by a test.

    A value that is neither member (a stray string) is treated as ``BLOCKED``.
    """

    BLOCKED = "blocked"
    OWNER_MANAGEMENT = "owner_management"


def manage_url() -> str:
    """Absolute URL of the account page that resumes a suspended workspace."""
    base_url = get_settings().frontend_url.strip().rstrip("/")
    return f"{base_url}{MANAGE_PATH}"


def _as_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _workspace_id_of(workspace_or_id: Workspace | UUID | str | None) -> UUID | None:
    if workspace_or_id is None:
        return None
    if isinstance(workspace_or_id, Workspace):
        return _as_uuid(workspace_or_id.id)
    return _as_uuid(workspace_or_id)


async def slot_suspended(db: AsyncSession, workspace_or_id: Workspace | UUID | str | None) -> bool:
    """Whether the workspace is slot-suspended now.

    ``False`` without any statement unless the mode is ``enforce``, and for
    ``None`` or a value that is not an id (the caller's own not-found handling
    owns that case). Otherwise one indexed lookup; never cached across calls.
    """
    if not workspace_slots_enforced():
        return False
    workspace_id = _workspace_id_of(workspace_or_id)
    if workspace_id is None:
        return False
    return workspace_id in await suspended_workspace_ids(db, [workspace_id])


async def ensure_slot_active(
    db: AsyncSession,
    workspace_or_id: Workspace | UUID | str | None,
    *,
    user_id: str | None,
    policy: SlotPolicy = SlotPolicy.BLOCKED,
    outsider: bool = False,
) -> None:
    """Raise ``WorkspaceSlotSuspendedError`` (403 ``SLOT-001``) for a suspended workspace.

    Call it after the caller is authorized for the workspace, so the refusal
    never answers for a workspace or context the caller could not reach.

    Args:
        db: Session (read only).
        workspace_or_id: The workspace whose data the call reads or writes —
            the target context's workspace, which may differ from the caller's
            session workspace. ``None`` passes.
        user_id: The caller. The owner is told where to resume it
            (``manage_url``), a member to ask the owner; anyone else, or
            ``None``, gets the gate only.
        policy: ``OWNER_MANAGEMENT`` lets the workspace's owner through.
        outsider: The caller is never a member (a public reader, a share-key
            reader, a management grant): always the redacted refusal.

    Raises:
        WorkspaceSlotSuspendedError: the workspace is suspended and the caller
            is not let through.
    """
    if not workspace_slots_enforced():
        return
    workspace_id = _workspace_id_of(workspace_or_id)
    if workspace_id is None or not await slot_suspended(db, workspace_id):
        return
    if outsider or user_id is None:
        _log_refusal(workspace_id, user_id, audience="outsider", policy=policy)
        raise WorkspaceSlotSuspendedError.for_outsider()
    if isinstance(workspace_or_id, Workspace):
        owner_user_id: str | None = workspace_or_id.owner_user_id
    else:
        owner_user_id = await db.scalar(
            select(Workspace.owner_user_id).where(Workspace.id == workspace_id)
        )
    if owner_user_id is not None and owner_user_id == user_id:
        # Only the enum member lets the owner through; anything else fails closed.
        if policy is SlotPolicy.OWNER_MANAGEMENT:
            return
        _log_refusal(workspace_id, user_id, audience="owner", policy=policy)
        raise WorkspaceSlotSuspendedError.for_owner(workspace_id, manage_url())
    if await is_workspace_member(db, workspace_id, user_id):
        _log_refusal(workspace_id, user_id, audience="member", policy=policy)
        raise WorkspaceSlotSuspendedError.for_member(workspace_id)
    _log_refusal(workspace_id, user_id, audience="outsider", policy=policy)
    raise WorkspaceSlotSuspendedError.for_outsider()


def _log_refusal(
    workspace_id: UUID, user_id: str | None, *, audience: str, policy: SlotPolicy
) -> None:
    logger.info(
        "workspace_slot_refused",
        workspace_id=str(workspace_id),
        user_id=user_id,
        audience=audience,
        policy=str(policy),
    )


def active_workspace_predicate(
    workspace_id_column: ColumnElement[UUID] | None = None,
) -> ColumnElement[bool] | None:
    """SQL twin of the gate: the row's workspace is not slot-suspended.

    ``None`` unless the mode is ``enforce``: the caller then adds nothing, so
    no statement changes and the slot tables are never referenced.

    Args:
        workspace_id_column: The column holding the workspace id of the rows
            being filtered (``Memory.workspace_id``, ``Context.workspace_id``
            …). ``None`` when the statement selects from ``Workspace`` itself;
            the predicate then correlates to that ``Workspace``.
    """
    if not workspace_slots_enforced():
        return None
    if workspace_id_column is None:
        return not_(suspension_in_force())
    suspended = (
        select(Workspace.id)
        .where(Workspace.id == workspace_id_column, suspension_in_force())
        .correlate_except(Workspace)
    )
    return not_(suspended.exists())
