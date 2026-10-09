"""Capacity-over lock for Free workspaces (#1941).

A workspace that comes back to Free from a subscription keeps every memory
and file it stored on the paid plan. Without a lock it could keep searching
all of them on Free, so a workspace that is over the Free capacity is locked
until it is cleaned up or re-subscribed:

* **Locked** when the plan is Free, the entitlement came from the billing
  service (``entitlement_source == external_billing`` — a self-hosted or
  admin-managed Free workspace is never locked), and the live memory count is
  over ``effective_memory_limit`` OR the stored file bytes are over
  ``effective_storage_limit_bytes``.
* **Blocked while locked**: reads (recall, reference, explore, pinned and
  guardrail loads …), writes (remember, update, uploads, context creation).
* **Still allowed**: listing, deleting (memories, contexts, files), export,
  usage and plan pages — everything the owner needs to get back under the cap.

The state is computed at gate time from two cheap queries (an indexed COUNT
and one primary-key row) and never stored, so deleting down to the cap or a
plan push unlocks on the very next call with nothing to invalidate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import PlanName
from config.settings import get_settings
from models.auth import ENTITLEMENT_SOURCE_EXTERNAL_BILLING, Context, Workspace, WorkspaceMember
from models.file_objects import FileObject, WorkspaceStorageUsage
from models.memory import Memory
from utils.exceptions import CapacityLockedError

# The web page where the owner sees the plan, the usage and the way back.
CLEANUP_PATH = "/workspace/settings/plan"


def cleanup_url() -> str:
    """Absolute URL of the plan/usage page a locked workspace is sent to."""
    base_url = get_settings().frontend_url.strip().rstrip("/")
    return f"{base_url}{CLEANUP_PATH}"


@dataclass(frozen=True)
class CapacityLock:
    """How far a locked workspace is over its Free capacity.

    ``over_memories`` / ``over_bytes`` are 0 on an axis that is within its
    limit, so a client can render "remove N memories / X MB" from them alone.
    """

    memory_count: int
    memory_limit: int
    over_memories: int
    used_bytes: int
    storage_limit_bytes: int
    over_bytes: int
    cleanup_url: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_error(self) -> CapacityLockedError:
        return CapacityLockedError(**self.as_dict())


def is_lock_candidate(workspace: Workspace) -> bool:
    """Whether the plan and provenance alone could put ``workspace`` in the lock.

    No query: callers use it to skip the two counts for every paid,
    self-hosted or admin-managed workspace.
    """
    return (
        workspace.plan_name == PlanName.FREE
        and workspace.entitlement_source == ENTITLEMENT_SOURCE_EXTERNAL_BILLING
    )


async def _bytes_in_deleted_contexts(db: AsyncSession, workspace_id: UUID) -> int:
    """Bytes of live uploaded files bound to a soft-deleted context.

    ``delete_context`` keeps the context's files (the context can be restored
    from its rows, ``services/context_restore.py``), so they stay in
    ``workspace_storage_usage``. But the owner can no longer see them —
    ``list_files`` and the storage page only show files of live contexts — so
    counting them would leave a storage lock the owner has no way to lift by
    deleting. The lock's figure therefore excludes them; the counter itself
    (and the upload quota) are unchanged.
    """
    deleted_contexts = select(Context.id).where(
        Context.workspace_id == workspace_id,
        Context.deleted_at.is_not(None),
    )
    total = await db.scalar(
        select(func.coalesce(func.sum(FileObject.size_bytes), 0)).where(
            FileObject.workspace_id == workspace_id,
            FileObject.deleted_at.is_(None),
            FileObject.status == "uploaded",
            FileObject.context_id.in_(deleted_contexts),
        )
    )
    return int(total or 0)


async def capacity_lock_state(db: AsyncSession, workspace: Workspace) -> CapacityLock | None:
    """The lock on ``workspace``, or ``None`` when it is not locked.

    Args:
        db: Session to count with.
        workspace: The workspace whose data is being read or written — the
            TARGET context's workspace, which may differ from the caller's
            session workspace.

    Returns:
        A :class:`CapacityLock` when over capacity, else ``None``.
    """
    if not is_lock_candidate(workspace):
        return None
    return await _measure(
        db,
        workspace.id,
        memory_limit=int(workspace.effective_memory_limit),
        storage_limit=int(workspace.effective_storage_limit_bytes),
    )


async def projected_free_capacity(db: AsyncSession, workspace: Workspace) -> CapacityLock | None:
    """The lock ``workspace`` WOULD be in once it is back on Free, or ``None``.

    For the pre-expiry notice: the workspace is still on its paid plan, so its
    own effective limits do not apply. The Free tier's limits are taken with
    the bonuses a downgrade keeps — the rule ``DowngradeEligibilityService``
    applies (addons and the referral bonus are retained, a zero-base tier
    never stacks them). The counting is the lock's own, including the
    deleted-context storage exclusion, so the notice and the lock agree.
    """
    from config.plan_tiers import PLAN_TIERS
    from models.auth import _zero_floor

    free = PLAN_TIERS[PlanName.FREE]
    memory_limit = _zero_floor(
        free.memory_limit,
        (workspace.addon_memory_bonus or 0) + (workspace.referral_memory_bonus or 0),
    )
    storage_limit = _zero_floor(
        free.storage_limit_bytes, (workspace.addon_storage_bonus_mb or 0) * 1024 * 1024
    )
    return await _measure(db, workspace.id, memory_limit=memory_limit, storage_limit=storage_limit)


async def _measure(
    db: AsyncSession, workspace_id: UUID, *, memory_limit: int, storage_limit: int
) -> CapacityLock | None:
    """Count the workspace against the given limits; ``None`` when within both."""
    memory_count = int(
        await db.scalar(
            select(func.count(Memory.id)).where(
                Memory.workspace_id == workspace_id,
                Memory.deleted_at.is_(None),
            )
        )
        or 0
    )
    used_bytes = int(
        await db.scalar(
            select(WorkspaceStorageUsage.used_bytes).where(
                WorkspaceStorageUsage.workspace_id == workspace_id
            )
        )
        or 0
    )
    if 0 <= storage_limit < used_bytes:
        # Only on the over-storage path, so an unlocked workspace pays nothing.
        used_bytes -= await _bytes_in_deleted_contexts(db, workspace_id)
    # A negative limit would mean "unlimited"; Free has none today, but a
    # settings override must not lock a workspace against an unlimited cap.
    over_memories = max(0, memory_count - memory_limit) if memory_limit >= 0 else 0
    over_bytes = max(0, used_bytes - storage_limit) if storage_limit >= 0 else 0
    if over_memories == 0 and over_bytes == 0:
        return None
    return CapacityLock(
        memory_count=memory_count,
        memory_limit=memory_limit,
        over_memories=over_memories,
        used_bytes=used_bytes,
        storage_limit_bytes=storage_limit,
        over_bytes=over_bytes,
        cleanup_url=cleanup_url(),
    )


async def is_workspace_member(db: AsyncSession, workspace_id: UUID, user_id: str) -> bool:
    """Whether ``user_id`` holds a membership row in ``workspace_id``."""
    found = await db.scalar(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    return found is not None


async def ensure_not_capacity_locked(
    db: AsyncSession,
    workspace_or_id: Workspace | UUID | str | None,
    *,
    user_id: str | None = None,
    outsider: bool = False,
) -> None:
    """Raise :class:`CapacityLockedError` when the workspace is locked.

    ``None`` or an unknown id passes: the caller's own not-found / permission
    handling owns that case, and the lock must never turn into an existence
    oracle.

    Args:
        db: Session.
        workspace_or_id: The target workspace (model or id).
        user_id: The caller, when the workspace came from a client-supplied
            id (a context, a memory, a file). A caller who is not a member of
            the locked workspace — a public-context reader — is refused
            without the counts or the cleanup URL, which describe someone
            else's workspace. ``None`` when the workspace is the caller's own
            authenticated session workspace.
        outsider: The caller is never a member — a share-key reader, whose
            principal carries the key creator's user id. Always refused with
            the redacted form.
    """
    if workspace_or_id is None:
        return
    if isinstance(workspace_or_id, Workspace):
        workspace: Workspace | None = workspace_or_id
    else:
        try:
            workspace_id = (
                workspace_or_id if isinstance(workspace_or_id, UUID) else UUID(workspace_or_id)
            )
        except (ValueError, TypeError, AttributeError):
            return
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return
    await _raise_if_locked(db, workspace, user_id, outsider=outsider)


async def _raise_if_locked(
    db: AsyncSession, workspace: Workspace, user_id: str | None, *, outsider: bool = False
) -> None:
    lock = await capacity_lock_state(db, workspace)
    if lock is None:
        return
    if outsider:
        raise CapacityLockedError.for_outsider()
    # The membership read runs only for a locked workspace — never on the hot path.
    if user_id is not None and not await is_workspace_member(db, workspace.id, user_id):
        raise CapacityLockedError.for_outsider()
    raise lock.to_error()


def lock_candidate_predicate() -> ColumnElement[bool]:
    """SQL twin of :func:`is_lock_candidate`.

    Lets a caller that resolves workspaces from ids fetch only the ones that
    could be locked, so a paid / self-hosted / admin-managed workspace costs
    no more than the one lookup it needed anyway.
    """
    return and_(
        Workspace.plan_name == PlanName.FREE,
        Workspace.entitlement_source == ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    )


def _as_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def ensure_contexts_not_capacity_locked(
    db: AsyncSession,
    context_ids: Iterable[UUID | str | None],
    *,
    user_id: str | None = None,
    outsider: bool = False,
) -> None:
    """:func:`ensure_not_capacity_locked` for every workspace owning ``context_ids``.

    One query for the whole set, deduplicated per workspace and filtered to
    lock candidates in SQL: contexts of paid workspaces add no further read.
    """
    ids = {cid for raw in context_ids if raw is not None and (cid := _as_uuid(raw)) is not None}
    if not ids:
        return
    workspaces = (
        await db.scalars(
            select(Workspace)
            .where(
                Workspace.id.in_(select(Context.workspace_id).where(Context.id.in_(ids))),
                lock_candidate_predicate(),
            )
            .order_by(Workspace.id)
        )
    ).all()
    for workspace in workspaces:
        await _raise_if_locked(db, workspace, user_id, outsider=outsider)


async def ensure_context_not_capacity_locked(
    db: AsyncSession,
    context_id: UUID | str | None,
    *,
    user_id: str | None = None,
    outsider: bool = False,
) -> None:
    """:func:`ensure_contexts_not_capacity_locked` for one context."""
    await ensure_contexts_not_capacity_locked(db, [context_id], user_id=user_id, outsider=outsider)
