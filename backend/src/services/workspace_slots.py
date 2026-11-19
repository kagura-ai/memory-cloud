"""Workspace slot suspension: the planner and the transition (Issue #2918).

An owner's allowance is the #1550 cap (``utils.plan_resolver``: 1 + slot bonus
+ the grant of the highest tier they own) or, when more of their workspaces are
under a billing contract than that, the number of contracted workspaces: a
workspace somebody pays for is never suspended. An event that ends a paid
contract, lowers a plan's owned-workspace grant or lowers the slot bonus (and
does not raise the allowance) suspends the excess owned workspaces over the
allowance, their data kept; nothing else suspends, and whenever slots return
they resume. Only owned workspaces count; membership in somebody else's
workspace takes no slot.

Pieces:

- ``plan_active_set``: the pure planner. Which workspaces stay active, in a
  deterministic priority order: contracted ones, then the owner's saved
  selection in its order, then the ones active now, then the rest; inside each
  group the owner's current workspace, then the higher plan, then the older
  workspace, then the id. Only a drop (``is_allowance_drop``) may shrink the
  active set; every other event keeps at least the workspaces active now.
- ``reconcile_workspace_slots``: the only writer of ``workspace_slot_states``
  and ``workspace_slot_suspensions``. It runs under the per-owner advisory lock
  (``owner_slot_lock``, the lock of the workspace-creation gate) and recomputes
  from the committed state.
- ``get_slot_overview`` / ``preview_selection`` / ``save_selection``: the
  account API's reads and its one write.
- ``suspended_workspace_ids`` / ``owner_selection_required`` /
  ``suspension_in_force``: the read-time rule for other modules.

``WORKSPACE_SLOT_SUSPENSION`` (``workspace_slot_mode``): ``off`` (default) runs
no statement on the slot tables; ``shadow`` logs what would be suspended,
resumes what should resume and never suspends; ``enforce`` suspends.

Lock order (no deadlock): a workspace row, then the owner lock (taken without
waiting while a row lock is held), then the slot-table rows. Reconcile takes no
workspace or ``users`` row lock.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import and_, delete, exists, not_, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

import config.settings as _config_settings
from config.plan_tiers import PLAN_ORDER, PlanName, get_plan_tier, plan_rank
from db.base import isolated_session
from models.auth import ENTITLEMENT_SOURCE_EXTERNAL_BILLING, User, Workspace
from models.workspace_slots import (
    SLOT_SUSPENSION_REASON_ALLOWANCE,
    WorkspaceSlotState,
    WorkspaceSlotSuspension,
)
from services.workspace_locks import (
    NOWAIT_LOCK_BACKOFF_SECONDS,
    NOWAIT_LOCK_TRIES,
    OWNER_LOCK_PREFIX,
    owner_lock_key,
)
from utils.datetime import utcnow
from utils.db_helpers import is_lock_not_available
from utils.exceptions import (
    NotFoundException,
    ValidationError,
    WorkspaceSlotsBusyError,
    WorkspaceSlotsFullError,
    WorkspaceSlotTransferRefusedError,
    WorkspaceSlotVersionConflictError,
)
from utils.logger import get_logger
from utils.plan_resolver import (
    WorkspaceCapSummary,
    cap_on_tier,
    next_tier_with_more_workspaces,
    resolve_workspace_cap,
)

__all__ = [
    "OWNER_SLOT_LOCK_PREFIX",
    "OwnedWorkspace",
    "PaidPlansEndOutcome",
    "PreviewScenario",
    "ReconcileOutcome",
    "SelectionPreview",
    "SlotAllowance",
    "SlotAllowanceView",
    "SlotCause",
    "SlotChange",
    "SlotChanges",
    "SlotMode",
    "SlotOverview",
    "SlotPlan",
    "SlotSelection",
    "SlotStatus",
    "SlotWorkspace",
    "billing_contract_active",
    "change_is_non_decreasing",
    "get_slot_overview",
    "is_allowance_drop",
    "owner_selection_required",
    "owner_slot_lock",
    "owner_slot_lock_key",
    "plan_active_set",
    "preview_selection",
    "reconcile_workspace_slots",
    "save_selection",
    "seed_default_suspension",
    "slot_allowance",
    "suspended_workspace_ids",
    "suspension_in_force",
    "workspace_slot_mode",
    "workspace_slots_enforced",
    # Step 2 (lane A's triggers, sweep, CLI and erasure).
    "PlanChangeSlots",
    "SWEEP_BATCH",
    "SweepOutcome",
    "assert_slot_tables_ready",
    "check_transfer_recipient",
    "delete_owner_slot_rows",
    "finish_plan_change",
    "prepare_plan_change",
    "reconcile_after_commit",
    "reconcile_plan_change",
    "slot_tables_present",
    "sweep_workspace_slots",
]

logger = get_logger(__name__)

SlotMode = Literal["off", "shadow", "enforce"]
# The event a reconcile runs for. The first five may suspend (and are what a
# suspension row's ``cause`` records); the others only resume and prune.
SlotCause = Literal[
    "billing_snapshot",  # ordered billing snapshot (integration entitlements)
    "billing_plan",  # legacy billing plan push
    "admin_plan",  # system-admin plan change
    "slot_bonus",  # system-admin slot-bonus change
    "selection",  # the owner saved a selection (a swap)
    "workspace_delete",
    "ownership_transfer",
    "sweep",
    "cli",
]
PreviewScenario = Literal["current", "paid_plans_end"]
SlotStatus = Literal["active", "slot_suspended"]

# The per-owner advisory lock is the workspace-creation gate's lock
# (``QuotaService._acquire_workspace_create_lock``) and the slot-bonus PATCH's:
# every path that changes what the owner's allowance or owned set is takes it.
OWNER_SLOT_LOCK_PREFIX: Final = OWNER_LOCK_PREFIX


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


def workspace_slot_mode() -> SlotMode:
    """``WORKSPACE_SLOT_SUSPENSION`` of this process (settings only, no I/O).

    Read at call time, so a test that swaps ``config.settings.get_settings``
    is seen here.
    """
    return _config_settings.get_settings().workspace_slot_suspension


def workspace_slots_enforced() -> bool:
    """True when suspensions are honoured (mode ``enforce``)."""
    return workspace_slot_mode() == "enforce"


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def billing_contract_active(plan_name: str | None, entitlement_source: str | None) -> bool:
    """``Workspace.has_active_billing_contract`` on plain values (#1940).

    True while the external billing service owns the entitlement on a paid
    tier, a scheduled cancellation and the payment grace included.
    """
    return entitlement_source == ENTITLEMENT_SOURCE_EXTERNAL_BILLING and plan_name != PlanName.FREE


def _owned_workspace_grant(plan_name: str | None) -> int:
    # Unknown or missing names rank as the lowest tier, as ``plan_rank`` does.
    return get_plan_tier(PLAN_ORDER[plan_rank(plan_name)]).owned_workspace_grant


@dataclass(frozen=True, slots=True)
class OwnedWorkspace:
    """One live workspace the owner owns, as the planner sees it.

    Attributes:
        id: Workspace id.
        plan_name: Its plan (never changed by suspension).
        entitlement_source: ``external_billing`` or ``admin_grant``.
        created_at: Creation time (naive UTC); older ranks first.
        suspended: Suspended now (an in-force suspension row for this owner);
            ``False`` = active now.
    """

    id: UUID
    plan_name: str
    entitlement_source: str
    created_at: datetime
    suspended: bool = False

    @property
    def under_billing_contract(self) -> bool:
        """Somebody pays for it: never suspended."""
        return billing_contract_active(self.plan_name, self.entitlement_source)


@dataclass(frozen=True, slots=True)
class SlotChange:
    """What the triggering event changed, as it was BEFORE the event.

    The "before" allowance substitutes these values; a field left ``None``
    did not change. ``workspace_id`` names the workspace whose plan or
    entitlement source changed; ``previous_slot_bonus`` is the owner's slot
    bonus before a bonus change.
    """

    workspace_id: UUID | None = None
    previous_plan_name: str | None = None
    previous_entitlement_source: str | None = None
    previous_slot_bonus: int | None = None


@dataclass(frozen=True, slots=True)
class SlotAllowance:
    """The #1550 cap and the effective allowance of one owner.

    Attributes:
        summary: The cap and its parts (``resolve_workspace_cap``).
        contracted_count: Owned workspaces under a billing contract.
        allowance: ``max(summary.cap, contracted_count)``.
    """

    summary: WorkspaceCapSummary
    contracted_count: int
    allowance: int


def slot_allowance(owned: Sequence[OwnedWorkspace], slot_bonus: int) -> SlotAllowance:
    """The owner's cap (#1550 formula) and effective allowance. Pure.

    Suspended workspaces count in ``owned_count`` and in the tier grant, as
    they do for the creation gate.
    """
    summary = resolve_workspace_cap(len(owned), slot_bonus, [w.plan_name for w in owned])
    contracted = sum(1 for w in owned if w.under_billing_contract)
    return SlotAllowance(
        summary=summary, contracted_count=contracted, allowance=max(summary.cap, contracted)
    )


def is_allowance_drop(
    owned: Sequence[OwnedWorkspace], slot_bonus: int, change: SlotChange | None
) -> bool:
    """True when the event is a drop: it may suspend the excess. Pure.

    A drop is an event that can lower the allowance (the changed workspace
    stopped being under a billing contract or its plan's owned-workspace
    grant went down, see ``change_is_non_decreasing``; or the slot bonus went
    down) and did not raise it. An owner above the allowance is then cut to
    it, also when the allowance stayed the same; an event that raises the
    allowance (a billed Basic comped to a granted Pro) never suspends.

    ``owned`` and ``slot_bonus`` are the state AFTER the event; the "before"
    side substitutes ``change``'s previous plan and entitlement source of the
    changed workspace (when the owner still owns it) or the previous bonus.
    """
    if change is None:
        return False
    before_owned = list(owned)
    can_lower = False
    if change.workspace_id is not None:
        changed = next((w for w in owned if w.id == change.workspace_id), None)
        if changed is not None:
            previous_plan = (
                change.previous_plan_name
                if change.previous_plan_name is not None
                else changed.plan_name
            )
            previous_source = (
                change.previous_entitlement_source
                if change.previous_entitlement_source is not None
                else changed.entitlement_source
            )
            can_lower = not change_is_non_decreasing(
                previous_plan, previous_source, changed.plan_name, changed.entitlement_source
            )
            before_owned = [
                dataclasses.replace(w, plan_name=previous_plan, entitlement_source=previous_source)
                if w.id == change.workspace_id
                else w
                for w in owned
            ]
    before_bonus = slot_bonus
    if change.previous_slot_bonus is not None:
        before_bonus = change.previous_slot_bonus
        can_lower = can_lower or slot_bonus < before_bonus
    if not can_lower:
        return False
    after = slot_allowance(owned, slot_bonus).allowance
    before = slot_allowance(before_owned, before_bonus).allowance
    return after <= before


def change_is_non_decreasing(
    previous_plan_name: str | None,
    previous_entitlement_source: str | None,
    new_plan_name: str | None,
    new_entitlement_source: str | None,
) -> bool:
    """True when one workspace's plan change cannot lower its owner's allowance. Pure.

    The new plan's owned-workspace grant is not below the old one AND the
    workspace did not stop being under a billing contract. Needs no sibling
    state, so a trigger may decide it before it holds the owner lock.
    """
    if _owned_workspace_grant(new_plan_name) < _owned_workspace_grant(previous_plan_name):
        return False
    return not (
        billing_contract_active(previous_plan_name, previous_entitlement_source)
        and not billing_contract_active(new_plan_name, new_entitlement_source)
    )


@dataclass(frozen=True, slots=True)
class SlotPlan:
    """The planner's result.

    Attributes:
        active: Workspaces that stay or become active, in priority order.
        suspended: The others, in priority order (first = next to resume).
        active_slots: How many may be active (``allowance`` on a drop, else
            ``max(allowance, active now)``).
        allowance: ``max(cap, contracted_count)``.
        contracted_count: Owned workspaces under a billing contract.
        drop: Whether the plan was made for a drop.
        unmet: Ids of ``keep_ids`` (owned, not contracted) left suspended.
    """

    active: tuple[UUID, ...]
    suspended: tuple[UUID, ...]
    active_slots: int
    allowance: int
    contracted_count: int
    drop: bool
    unmet: tuple[UUID, ...]


def _default_order(
    workspaces: Iterable[OwnedWorkspace], current_workspace_id: UUID | None
) -> list[OwnedWorkspace]:
    return sorted(
        workspaces,
        key=lambda w: (
            w.id != current_workspace_id,
            -plan_rank(w.plan_name),
            w.created_at,
            w.id,
        ),
    )


def plan_active_set(
    owned: Sequence[OwnedWorkspace],
    cap: int,
    *,
    drop: bool,
    keep_ids: Sequence[UUID] = (),
    current_workspace_id: UUID | None = None,
) -> SlotPlan:
    """Decide which owned workspaces are active. Pure and deterministic.

    ``allowance = max(cap, contracted)``; ``n = allowance`` on a drop, else
    ``max(allowance, active now)``. Priority: workspaces under a billing
    contract, then ``keep_ids`` in the given order (ids not owned, duplicates
    and contracted ids are ignored), then the workspaces active now, then the
    rest; inside each group except ``keep_ids``: ``current_workspace_id`` first,
    then the higher plan, the older ``created_at``, the id. The first ``n``
    are active.

    Properties (pinned by ``tests/services/test_workspace_slots_planner.py``):
    a contracted workspace is always active; without a drop the active count
    never shrinks; on a drop it is ``min(owned, allowance)``; planning the
    result again changes nothing; the input order does not matter.

    Args:
        owned: The owner's live owned workspaces (unique ids).
        cap: The owner's #1550 cap (``slot_allowance(...).summary.cap``).
        drop: The event was a drop (``is_allowance_drop``).
        keep_ids: The owner's ordered selection.
        current_workspace_id: The owner's ``users.current_workspace_id``.

    Returns:
        The plan.
    """
    by_id = {w.id: w for w in owned}
    contracted = [w for w in owned if w.under_billing_contract]
    allowance = max(cap, len(contracted))
    active_now = sum(1 for w in owned if not w.suspended)
    n = allowance if drop else max(allowance, active_now)

    order: list[UUID] = [w.id for w in _default_order(contracted, current_workspace_id)]
    placed = set(order)
    kept: list[UUID] = []
    for wid in keep_ids:
        if wid in by_id and wid not in placed:
            order.append(wid)
            placed.add(wid)
            kept.append(wid)
    order.extend(
        w.id
        for w in _default_order(
            (w for w in owned if not w.suspended and w.id not in placed), current_workspace_id
        )
    )
    placed.update(order)
    order.extend(
        w.id for w in _default_order((w for w in owned if w.id not in placed), current_workspace_id)
    )

    active = tuple(order[:n])
    suspended = tuple(order[n:])
    suspended_set = set(suspended)
    return SlotPlan(
        active=active,
        suspended=suspended,
        active_slots=n,
        allowance=allowance,
        contracted_count=len(contracted),
        drop=drop,
        unmet=tuple(wid for wid in kept if wid in suspended_set),
    )


# ---------------------------------------------------------------------------
# Read model (account API)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlotAllowanceView:
    """``allowance`` of the account API: the #1550 cap, its parts and the upsell.

    ``next_tier`` is the lowest tier granting more slots than ``tier``
    (``None`` on the top tier); ``cap_on_next_tier`` the cap there.
    """

    cap: int
    base: int
    slot_bonus: int
    tier_grant: int
    tier: str
    next_tier: str | None
    cap_on_next_tier: int | None


@dataclass(frozen=True, slots=True)
class SlotSelection:
    """The saved selection: owned ids in saved order, and when it was saved."""

    workspace_ids: tuple[UUID, ...]
    saved_at: datetime | None


@dataclass(frozen=True, slots=True)
class PaidPlansEndOutcome:
    """What would be active if every subscription of the owner ended now.

    Every workspace under a billing contract becomes Free; admin-granted
    tiers keep their plan; the saved selection applies.
    """

    cap: int
    active: tuple[UUID, ...]
    suspended: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class SlotWorkspace:
    """One owned workspace in the account API (``workspaces[]``)."""

    id: UUID
    name: str
    plan_name: str
    status: SlotStatus
    suspended_at: datetime | None
    under_billing_contract: bool
    is_current: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SlotOverview:
    """Everything ``GET /api/v1/me/workspace-slots`` returns (design 12.1).

    Attributes:
        allowance: The cap, its parts and the next tier.
        owned_count: Live owned workspaces.
        active_count: Of those, active (status ``active``).
        active_slots: ``max(cap, contracted_count, active_count)``.
        contracted_count: Owned workspaces under a billing contract.
        selection_required: A drop suspended workspaces the owner has not
            chosen, and something is still suspended.
        selection: The saved selection (live owned ids only).
        version: Sent back by ``save_selection`` (0 before the first change).
        if_paid_plans_end: ``None`` when nothing is under contract.
        workspaces: Every live owned workspace, oldest first (then by id).
    """

    allowance: SlotAllowanceView
    owned_count: int
    active_count: int
    active_slots: int
    contracted_count: int
    selection_required: bool
    selection: SlotSelection
    version: int
    if_paid_plans_end: PaidPlansEndOutcome | None
    workspaces: tuple[SlotWorkspace, ...]


@dataclass(frozen=True, slots=True)
class SlotChanges:
    """Relative to the active set now: what would be suspended and resumed."""

    suspend: tuple[UUID, ...]
    resume: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class SelectionPreview:
    """``POST /api/v1/me/workspace-slots/preview``: the outcome of a selection.

    ``cap`` is the #1550 cap in the scenario; ``unmet`` the selected ids that
    stay suspended; ``changes`` is relative to the active set now.
    """

    scenario: PreviewScenario
    cap: int
    active_slots: int
    active: tuple[UUID, ...]
    suspended: tuple[UUID, ...]
    unmet: tuple[UUID, ...]
    changes: SlotChanges


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """What one ``reconcile_workspace_slots`` call did.

    Attributes:
        mode: The mode it ran in (``off``: nothing ran).
        cause: The triggering event.
        drop: Whether the event was a drop (``is_allowance_drop``).
        suspended: Workspaces suspended by this call (``shadow``: would be).
        resumed: Workspaces resumed by this call.
        pruned: Rows removed for workspaces the owner no longer owns.
        version: The owner's state version after the call (``None``: no row).
    """

    mode: SlotMode
    cause: SlotCause
    drop: bool = False
    suspended: tuple[UUID, ...] = ()
    resumed: tuple[UUID, ...] = ()
    pruned: int = 0
    version: int | None = None


# ---------------------------------------------------------------------------
# Read-time rule (gates, flags)
# ---------------------------------------------------------------------------


def suspension_in_force() -> ColumnElement[bool]:
    """SQL: the ``workspaces`` row of the enclosing query is suspended.

    A suspension row counts only while the owner it was stamped with still
    owns the live workspace and the workspace is not under a billing contract,
    so a contracted workspace is never refused whatever the rows say. Builds an
    expression and runs nothing: use it in a statement only when the mode is
    ``enforce`` (``workspace_slots_enforced``).
    """
    return and_(
        exists().where(
            WorkspaceSlotSuspension.workspace_id == Workspace.id,
            WorkspaceSlotSuspension.owner_user_id == Workspace.owner_user_id,
        ),
        Workspace.deleted_at.is_(None),
        not_(
            and_(
                Workspace.entitlement_source == ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
                Workspace.plan_name != PlanName.FREE,
            )
        ),
    )


async def suspended_workspace_ids(
    db: AsyncSession, workspace_ids: Iterable[UUID]
) -> frozenset[UUID]:
    """The ids among ``workspace_ids`` that are suspended now (one statement).

    Empty, without any statement, unless the mode is ``enforce`` or when no id
    is given.
    """
    ids = list(dict.fromkeys(workspace_ids))
    if not ids or not workspace_slots_enforced():
        return frozenset()
    rows = await db.execute(
        select(Workspace.id).where(Workspace.id.in_(ids), suspension_in_force())
    )
    return frozenset(rows.scalars().all())


async def owner_selection_required(db: AsyncSession, owner_user_id: str) -> bool:
    """``/auth/me`` ``workspace_selection_required`` for ``owner_user_id``.

    True while a drop left workspaces suspended that the owner has not
    chosen (``selection_required_since`` set) and one of their workspaces is
    still suspended. ``False``, without any statement, unless the mode is
    ``enforce``.
    """
    if not workspace_slots_enforced():
        return False
    stmt = select(
        exists().where(
            WorkspaceSlotState.owner_user_id == owner_user_id,
            WorkspaceSlotState.selection_required_since.is_not(None),
        )
        & exists().where(
            Workspace.owner_user_id == owner_user_id,
            suspension_in_force(),
        )
    )
    return bool((await db.execute(stmt)).scalar())


# ---------------------------------------------------------------------------
# Owner state reader (column selects, no row locks)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _OwnerState:
    """One owner's slot inputs, read with column selects (fresh values).

    ``owned[i].suspended`` is the read-time rule (``suspension_in_force``,
    evaluated in the same statement): an own row on a workspace not under a
    billing contract. ``own_rows`` holds every own row
    on an owned workspace (contracted ones included) with its time;
    ``foreign_rows`` the rows another owner left on an owned workspace;
    ``orphan_rows`` this owner's rows on workspaces they no longer own.
    """

    owned: tuple[OwnedWorkspace, ...]
    names: dict[UUID, str]
    slot_bonus: int
    current_workspace_id: UUID | None
    own_rows: dict[UUID, datetime]
    foreign_rows: tuple[UUID, ...]
    orphan_rows: tuple[UUID, ...]
    has_state: bool
    keep_ids: tuple[UUID, ...]
    selection_saved_at: datetime | None
    selection_required_since: datetime | None
    version: int

    @property
    def owned_ids(self) -> frozenset[UUID]:
        return frozenset(w.id for w in self.owned)

    @property
    def suspended_now(self) -> frozenset[UUID]:
        return frozenset(w.id for w in self.owned if w.suspended)


async def _read_owner(db: AsyncSession, owner_user_id: str) -> _OwnerState:
    user = (
        await db.execute(
            select(User.workspace_slot_bonus, User.current_workspace_id).where(
                User.user_id == owner_user_id
            )
        )
    ).one_or_none()
    rows = (
        await db.execute(
            select(
                Workspace.id,
                Workspace.name,
                Workspace.plan_name,
                Workspace.entitlement_source,
                Workspace.created_at,
                # The gate's read-time rule itself, so the overview, the
                # planner and every gate share one definition.
                suspension_in_force().label("suspended"),
            ).where(Workspace.owner_user_id == owner_user_id, Workspace.deleted_at.is_(None))
        )
    ).all()
    owned_ids = [r.id for r in rows]
    suspension_rows = (
        await db.execute(
            select(
                WorkspaceSlotSuspension.workspace_id,
                WorkspaceSlotSuspension.owner_user_id,
                WorkspaceSlotSuspension.suspended_at,
            ).where(
                or_(
                    WorkspaceSlotSuspension.owner_user_id == owner_user_id,
                    WorkspaceSlotSuspension.workspace_id.in_(owned_ids),
                )
            )
        )
    ).all()
    state = (
        await db.execute(
            select(
                WorkspaceSlotState.keep_workspace_ids,
                WorkspaceSlotState.selection_saved_at,
                WorkspaceSlotState.selection_required_since,
                WorkspaceSlotState.version,
            ).where(WorkspaceSlotState.owner_user_id == owner_user_id)
        )
    ).one_or_none()

    owned_set = set(owned_ids)
    own_rows: dict[UUID, datetime] = {}
    foreign: list[UUID] = []
    orphans: list[UUID] = []
    for row in suspension_rows:
        if row.workspace_id in owned_set:
            if row.owner_user_id == owner_user_id:
                own_rows[row.workspace_id] = row.suspended_at
            else:
                foreign.append(row.workspace_id)
        elif row.owner_user_id == owner_user_id:
            orphans.append(row.workspace_id)
    owned = tuple(
        OwnedWorkspace(
            id=r.id,
            plan_name=r.plan_name,
            entitlement_source=r.entitlement_source,
            created_at=r.created_at,
            suspended=bool(r.suspended),
        )
        for r in rows
    )
    return _OwnerState(
        owned=owned,
        names={r.id: r.name for r in rows},
        slot_bonus=int(user.workspace_slot_bonus) if user is not None else 0,
        current_workspace_id=user.current_workspace_id if user is not None else None,
        own_rows=own_rows,
        foreign_rows=tuple(sorted(foreign)),
        orphan_rows=tuple(sorted(orphans)),
        has_state=state is not None,
        keep_ids=tuple(state.keep_workspace_ids or ()) if state is not None else (),
        selection_saved_at=state.selection_saved_at if state is not None else None,
        selection_required_since=state.selection_required_since if state is not None else None,
        version=int(state.version) if state is not None else 0,
    )


# ---------------------------------------------------------------------------
# Row writers (Core statements; multi-row deletes lock in workspace_id order)
# ---------------------------------------------------------------------------


async def _delete_suspensions(db: AsyncSession, *conditions: ColumnElement[bool]) -> int:
    """Delete the matching rows, locking them in ``workspace_id`` order first."""
    locked = (
        select(WorkspaceSlotSuspension.workspace_id)
        .where(*conditions)
        .order_by(WorkspaceSlotSuspension.workspace_id)
        .with_for_update()
    )
    result = await db.execute(
        delete(WorkspaceSlotSuspension).where(WorkspaceSlotSuspension.workspace_id.in_(locked))
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def _insert_suspensions(
    db: AsyncSession,
    owner_user_id: str,
    workspace_ids: Iterable[UUID],
    *,
    cause: SlotCause,
    now: datetime,
    replace: bool,
) -> None:
    values = [
        {
            "workspace_id": wid,
            "owner_user_id": owner_user_id,
            "reason": SLOT_SUSPENSION_REASON_ALLOWANCE,
            "cause": cause,
            "suspended_at": now,
        }
        for wid in sorted(workspace_ids)
    ]
    if not values:
        return
    stmt = pg_insert(WorkspaceSlotSuspension).values(values)
    if replace:
        # A row another owner left on the workspace (an ownership move whose
        # cleanup has not run yet) is taken over, never a key error.
        stmt = stmt.on_conflict_do_update(
            index_elements=[WorkspaceSlotSuspension.workspace_id],
            set_={
                "owner_user_id": stmt.excluded.owner_user_id,
                "cause": stmt.excluded.cause,
                "suspended_at": stmt.excluded.suspended_at,
            },
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[WorkspaceSlotSuspension.workspace_id])
    await db.execute(stmt)


async def _write_state(
    db: AsyncSession,
    owner_user_id: str,
    *,
    now: datetime,
    version: int,
    selection_required_since: datetime | None,
    keep_ids: Sequence[UUID] | None = None,
    selection_saved_at: datetime | None = None,
    insert: bool = True,
) -> None:
    """Insert or update the owner's state row (``keep_ids=None`` keeps the selection).

    ``insert=False`` only updates an existing row: a resume-only path never
    creates one, so a reconcile racing the erasure of its owner cannot
    re-create the row the erasure just deleted.
    """
    values: dict[str, object] = {
        "owner_user_id": owner_user_id,
        "selection_required_since": selection_required_since,
        "version": version,
        "updated_at": now,
    }
    update_set: dict[str, object] = {
        "selection_required_since": selection_required_since,
        "version": version,
        "updated_at": now,
    }
    if keep_ids is not None:
        values["keep_workspace_ids"] = list(keep_ids)
        values["selection_saved_at"] = selection_saved_at
        update_set["keep_workspace_ids"] = list(keep_ids)
        update_set["selection_saved_at"] = selection_saved_at
    if not insert:
        await db.execute(
            update(WorkspaceSlotState)
            .where(WorkspaceSlotState.owner_user_id == owner_user_id)
            .values(**update_set)
        )
        return
    stmt = pg_insert(WorkspaceSlotState).values(values)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[WorkspaceSlotState.owner_user_id], set_=update_set
        )
    )


async def _lock_and_delete(
    db: AsyncSession, owner_user_id: str, *, foreign: Sequence[UUID], own: Sequence[UUID]
) -> list[tuple[UUID, str]]:
    """Delete rows another owner left on ``foreign`` and this owner's rows on
    ``own``, after locking ALL of them in ONE statement in ``workspace_id``
    order: two owners whose rows cross (workspaces moved both ways) lock them
    in the same order and never deadlock.

    Returns:
        ``(workspace_id, owner_user_id)`` of every deleted row.
    """
    conditions: list[ColumnElement[bool]] = []
    if foreign:
        conditions.append(
            and_(
                WorkspaceSlotSuspension.workspace_id.in_(foreign),
                WorkspaceSlotSuspension.owner_user_id != owner_user_id,
            )
        )
    if own:
        conditions.append(
            and_(
                WorkspaceSlotSuspension.workspace_id.in_(own),
                WorkspaceSlotSuspension.owner_user_id == owner_user_id,
            )
        )
    if not conditions:
        return []
    locked = [
        (row.workspace_id, row.owner_user_id)
        for row in (
            await db.execute(
                select(WorkspaceSlotSuspension.workspace_id, WorkspaceSlotSuspension.owner_user_id)
                .where(or_(*conditions))
                .order_by(WorkspaceSlotSuspension.workspace_id)
                .with_for_update()
            )
        ).all()
    ]
    if locked:
        await db.execute(
            delete(WorkspaceSlotSuspension).where(
                WorkspaceSlotSuspension.workspace_id.in_([wid for wid, _ in locked])
            )
        )
    return locked


# ---------------------------------------------------------------------------
# Owner lock
# ---------------------------------------------------------------------------

_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"
_TRY_LOCK_SQL = "SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"
# How long a waiting acquire may wait: the workspace-creation gate's value.
_WAIT_LOCK_TIMEOUT = "5s"
# How long a shadow reconcile, the sweep or a post-commit reconcile may wait on
# a slot-table row lock (an uncommitted erasure holds them, for example); a
# timeout counts as busy and the next sweep finishes the work.
_SLOT_ROW_LOCK_TIMEOUT = "100ms"


def owner_slot_lock_key(owner_user_id: str) -> str:
    """The advisory-lock key of ``owner_user_id`` (``workspace_locks.owner_lock_key``)."""
    return owner_lock_key(owner_user_id)


async def owner_slot_lock(
    db: AsyncSession,
    owner_user_id: str,
    *,
    wait: bool,
    tries: int = NOWAIT_LOCK_TRIES,
) -> bool:
    """Take the per-owner transaction advisory lock (released at commit/rollback).

    ``wait=True`` waits up to 5 s (``lock_timeout``, restored afterwards); only
    for a transaction that holds no row lock (the selection PUT as its first
    statement, the post-commit reconciles, the CLI). ``wait=False`` tries
    without waiting, ``tries`` times with the ``retry_nowait`` backoff of
    ``services.workspace_locks`` (about 0.75 s at most), for a transaction
    that holds a workspace row; ``tries=1`` is one try with no sleep (the
    sweep, ``shadow``). Either way the transaction stays usable when the lock
    is not obtained.

    Returns:
        True when the lock is held, False when it stayed busy.
    """
    key = owner_slot_lock_key(owner_user_id)
    if wait:
        previous = (await db.execute(text("SELECT current_setting('lock_timeout')"))).scalar()
        try:
            # The savepoint keeps the transaction usable after a timeout; a
            # lock taken inside a released savepoint stays with the
            # transaction.
            async with db.begin_nested():
                await db.execute(
                    text("SELECT set_config('lock_timeout', :value, true)"),
                    {"value": _WAIT_LOCK_TIMEOUT},
                )
                await db.execute(text(_LOCK_SQL), {"key": key})
        except DBAPIError as exc:
            if not is_lock_not_available(exc):
                raise
            return False
        await db.execute(
            text("SELECT set_config('lock_timeout', :value, true)"), {"value": previous}
        )
        return True
    attempts = max(tries, 1)
    for attempt in range(attempts):
        if (await db.execute(text(_TRY_LOCK_SQL), {"key": key})).scalar():
            return True
        if attempt + 1 < attempts:
            await asyncio.sleep(NOWAIT_LOCK_BACKOFF_SECONDS * 2**attempt)
    return False


# ---------------------------------------------------------------------------
# The transition
# ---------------------------------------------------------------------------


async def _bound_row_lock_waits(session: AsyncSession) -> None:
    """Cap this transaction's waits on slot-table row locks (resume-only paths).

    A wait longer than ``_SLOT_ROW_LOCK_TIMEOUT`` raises ``lock_not_available``,
    which the caller counts as busy. Transaction-scoped: ends with the commit.
    """
    await session.execute(
        text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": _SLOT_ROW_LOCK_TIMEOUT},
    )


def _selection_is_valid(state: _OwnerState) -> bool:
    """The owner saved a selection that still names a workspace they own.

    An empty saved list is the default order, not a choice: saving it clears
    the prompt, but a later drop that suspends something asks again.
    """
    if state.selection_saved_at is None:
        return False
    return any(k in state.owned_ids for k in state.keep_ids)


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    """What one transition does to an owner's suspension rows.

    Attributes:
        plan: The planner's result.
        drop: Whether the event was a drop (``is_allowance_drop``).
        clear: Workspaces planned active that hold an own suspension row (the
            row goes; contracted ones included).
        resumed: Of those, the ones that were not usable before the event
            (suspended by the read-time rule, or the changed workspace).
        suspend: Workspaces to write a row for (only with ``allow_suspend``):
            planned suspended and not suspended now.
    """

    plan: SlotPlan
    drop: bool
    clear: tuple[UUID, ...]
    resumed: tuple[UUID, ...]
    suspend: tuple[UUID, ...]


def plan_transition(
    owned: Sequence[OwnedWorkspace],
    own_row_ids: Iterable[UUID],
    slot_bonus: int,
    *,
    change: SlotChange | None,
    allow_suspend: bool,
    keep_ids: Sequence[UUID],
    current_workspace_id: UUID | None,
) -> TransitionPlan:
    """The transition's decision on pure inputs. Pure and deterministic.

    ``owned`` is as the reader builds it (``suspended`` = an own row and not
    under a billing contract); ``own_row_ids`` are the owned workspaces that
    hold an own row, contracted ones included. The workspace the event changed
    that still holds a row counts as suspended going in: it was not usable
    before the event, so a suspended Free workspace bought as Basic swaps out
    an active one instead of joining the active set for free (with
    ``allow_suspend``; a resume-only path resumes it and suspends nothing).
    """
    rows = frozenset(own_row_ids)
    allowance = slot_allowance(owned, slot_bonus)
    drop = allow_suspend and is_allowance_drop(owned, slot_bonus, change)
    held = change.workspace_id if change is not None else None
    if held not in rows:
        held = None
    planner_in = [dataclasses.replace(w, suspended=True) if w.id == held else w for w in owned]
    plan = plan_active_set(
        planner_in,
        allowance.summary.cap,
        drop=drop,
        keep_ids=keep_ids,
        current_workspace_id=current_workspace_id,
    )
    before = {w.id for w in planner_in if w.suspended}
    suspended_now = {w.id for w in owned if w.suspended}
    return TransitionPlan(
        plan=plan,
        drop=drop,
        clear=tuple(wid for wid in plan.active if wid in rows),
        resumed=tuple(wid for wid in plan.active if wid in before),
        suspend=(
            tuple(wid for wid in plan.suspended if wid not in suspended_now)
            if allow_suspend
            else ()
        ),
    )


async def _reconcile(
    db: AsyncSession,
    owner_user_id: str,
    *,
    cause: SlotCause,
    allow_suspend: bool,
    change: SlotChange | None,
    mode: SlotMode,
) -> ReconcileOutcome:
    now = utcnow()
    state = await _read_owner(db, owner_user_id)
    allowance = slot_allowance(state.owned, state.slot_bonus)
    decision = plan_transition(
        state.owned,
        state.own_rows,
        state.slot_bonus,
        change=change,
        allow_suspend=allow_suspend,
        keep_ids=state.keep_ids,
        current_workspace_id=state.current_workspace_id,
    )
    plan, drop, resumed = decision.plan, decision.drop, decision.resumed
    shadow = mode == "shadow"
    inserted = () if shadow else decision.suspend

    deleted = await _lock_and_delete(
        db,
        owner_user_id,
        foreign=state.foreign_rows,
        own=[*decision.clear, *state.orphan_rows],
    )
    orphans = set(state.orphan_rows)
    pruned = sum(1 for wid, owner in deleted if owner != owner_user_id or wid in orphans)
    if inserted:
        await _insert_suspensions(db, owner_user_id, inserted, cause=cause, now=now, replace=True)

    suspended_after = (state.suspended_now - set(resumed)) | set(inserted)
    since = state.selection_required_since
    if inserted and cause != "selection" and not _selection_is_valid(state):
        since = since or now
    if not suspended_after:
        since = None
    changed = bool(inserted or resumed)
    version: int | None = state.version if state.has_state else None
    write_state = changed or since != state.selection_required_since
    # A state row is created only where a row is inserted (a drop or a swap);
    # every other path updates an existing one and never re-creates it.
    may_insert = bool(inserted)
    if write_state and (state.has_state or may_insert):
        version = state.version + (1 if changed else 0)
        await _write_state(
            db,
            owner_user_id,
            now=now,
            version=version,
            selection_required_since=since,
            insert=may_insert,
        )

    fields = {
        "owner_user_id": owner_user_id,
        "cause": cause,
        "drop": drop,
        "cap": allowance.summary.cap,
        "allowance": allowance.allowance,
        "active_slots": plan.active_slots,
        "resumed": [str(w) for w in resumed],
        "pruned": pruned,
    }
    if shadow:
        would = decision.suspend
        if would or resumed or pruned:
            logger.info(
                "workspace_slots_would_change",
                would_suspend=[str(w) for w in would],
                **fields,
            )
        return ReconcileOutcome(
            mode=mode,
            cause=cause,
            drop=drop,
            suspended=would,
            resumed=resumed,
            pruned=pruned,
            version=version,
        )
    if changed or pruned:
        logger.info(
            "workspace_slots_reconciled",
            suspended=[str(w) for w in inserted],
            version=version,
            **fields,
        )
    return ReconcileOutcome(
        mode=mode,
        cause=cause,
        drop=drop,
        suspended=inserted,
        resumed=resumed,
        pruned=pruned,
        version=version,
    )


async def reconcile_workspace_slots(
    db: AsyncSession,
    owner_user_id: str,
    *,
    cause: SlotCause,
    allow_suspend: bool,
    change: SlotChange | None = None,
    mode: SlotMode | None = None,
) -> ReconcileOutcome:
    """Bring one owner's suspension rows in line with the planner. The only writer.

    The caller holds ``owner_slot_lock``. Reads the owner's live owned
    workspaces, slot bonus, current workspace and selection with column
    selects (fresh READ COMMITTED reads, no row locks), decides the drop with
    ``is_allowance_drop(owned, bonus, change)`` (only when ``allow_suspend``),
    runs ``plan_active_set`` and writes the difference: deletes rows of
    workspaces that resume, removes rows another owner left on workspaces this
    owner now owns, prunes this owner's rows for workspaces they no longer own,
    inserts rows for workspaces to suspend (only with ``allow_suspend``),
    maintains ``selection_required_since`` and bumps ``version`` when the
    active set changed. Never writes a plan, add-on, entitlement source,
    capacity stamp, current workspace or any data.

    Modes: ``off`` returns before any statement; ``shadow`` never inserts,
    still deletes rows that should resume, logs
    ``workspace_slots_would_change`` and runs in a savepoint so it cannot fail
    its caller; ``enforce`` writes and logs ``workspace_slots_reconciled``.
    The caller commits.

    Args:
        db: The caller's session (its transaction).
        owner_user_id: The owner.
        cause: The triggering event (logs; the ``cause`` of new rows).
        allow_suspend: The path may suspend (the triggers of a drop and the
            selection PUT); ``False`` = resume and prune only (post-commit
            paths, the sweep, the CLI).
        change: The event's previous values (``SlotChange``) for the drop test.
        mode: Overrides ``WORKSPACE_SLOT_SUSPENSION`` (the CLI's
            ``--even-when-off``); ``None`` = the setting.

    Returns:
        What changed (``shadow``: ``suspended`` lists what would have been).
    """
    mode = mode or workspace_slot_mode()
    if mode == "off":
        return ReconcileOutcome(mode="off", cause=cause)
    if mode == "shadow":
        try:
            previous = (await db.execute(text("SELECT current_setting('lock_timeout')"))).scalar()
            async with db.begin_nested():
                # Shadow never stalls its caller: a row lock that is not free
                # at once ends this savepoint (logged below), and the setting
                # is restored before the savepoint is released.
                await db.execute(
                    text("SELECT set_config('lock_timeout', :value, true)"),
                    {"value": _SLOT_ROW_LOCK_TIMEOUT},
                )
                outcome = await _reconcile(
                    db,
                    owner_user_id,
                    cause=cause,
                    allow_suspend=allow_suspend,
                    change=change,
                    mode=mode,
                )
                await db.execute(
                    text("SELECT set_config('lock_timeout', :value, true)"), {"value": previous}
                )
            return outcome
        except Exception as exc:  # noqa: BLE001 — shadow must never fail its caller
            logger.warning(
                "workspace_slots_shadow_failed",
                owner_user_id=owner_user_id,
                cause=cause,
                error_class=type(exc).__name__,
            )
            return ReconcileOutcome(mode=mode, cause=cause)
    return await _reconcile(
        db,
        owner_user_id,
        cause=cause,
        allow_suspend=allow_suspend,
        change=change,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Triggers that hold the changed workspace's row (snapshot, legacy push, admin plan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanChangeSlots:
    """A plan change's slot work, between the owner try-lock and the reconcile.

    Attributes:
        owner_user_id: The changed workspace's owner.
        cause: ``billing_snapshot``, ``billing_plan`` or ``admin_plan``.
        change: The previous plan and entitlement source of the workspace.
        non_decreasing: The change cannot lower the allowance
            (``change_is_non_decreasing``): the reconcile may be deferred.
        mode: The mode read once when the trigger started.
        locked: Whether the owner lock was obtained.
    """

    owner_user_id: str
    cause: SlotCause
    change: SlotChange
    non_decreasing: bool
    mode: SlotMode
    locked: bool


async def prepare_plan_change(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    owner_user_id: str,
    cause: SlotCause,
    previous_plan_name: str,
    previous_entitlement_source: str,
    new_plan_name: str,
    new_entitlement_source: str,
) -> PlanChangeSlots | None:
    """Take the owner lock for a plan change of a workspace whose row is held.

    Called after the change is written to the locked workspace row and before
    any other lock (the snapshot path calls it before
    ``stop_ineligible_workspace``). Try-lock only: ``enforce`` with the
    ``retry_nowait`` budget, ``shadow`` once. ``None`` in mode ``off`` (no
    statement).

    Raises:
        WorkspaceSlotsBusyError: ``enforce``, the lock stayed busy and the
            change may lower the allowance (409 ``RES-002``, retryable;
            nothing is committed).
    """
    mode = workspace_slot_mode()
    if mode == "off":
        return None
    non_decreasing = change_is_non_decreasing(
        previous_plan_name,
        previous_entitlement_source,
        new_plan_name,
        new_entitlement_source,
    )
    locked = await owner_slot_lock(
        db,
        owner_user_id,
        wait=False,
        tries=NOWAIT_LOCK_TRIES if mode == "enforce" else 1,
    )
    if not locked and mode == "enforce" and not non_decreasing:
        logger.warning(
            "workspace_slots_busy",
            owner_user_id=owner_user_id,
            workspace_id=str(workspace_id),
            cause=cause,
        )
        raise WorkspaceSlotsBusyError()
    return PlanChangeSlots(
        owner_user_id=owner_user_id,
        cause=cause,
        change=SlotChange(
            workspace_id=workspace_id,
            previous_plan_name=previous_plan_name,
            previous_entitlement_source=previous_entitlement_source,
        ),
        non_decreasing=non_decreasing,
        mode=mode,
        locked=locked,
    )


def _log_deferred(prepared: PlanChangeSlots, reason: str, error_class: str | None = None) -> None:
    logger.warning(
        "workspace_slots_deferred",
        owner_user_id=prepared.owner_user_id,
        workspace_id=str(prepared.change.workspace_id),
        cause=prepared.cause,
        reason=reason,
        error_class=error_class,
    )


async def finish_plan_change(
    db: AsyncSession, prepared: PlanChangeSlots | None
) -> ReconcileOutcome | None:
    """Reconcile after ``prepare_plan_change``, in the same transaction.

    - Lock not obtained (only a non-decreasing change or ``shadow`` gets
      here): log ``workspace_slots_deferred``; the delivery commits and the
      sweep finishes the resume.
    - Non-decreasing change in ``enforce``: in a savepoint; any error rolls
      the savepoint back and is deferred the same way.
    - Any other change in ``enforce``: an error fails the delivery and is
      logged as ``workspace_slots_reconcile_failed``.
    - ``shadow``: ``reconcile_workspace_slots`` never fails its caller.
    """
    if prepared is None:
        return None
    if not prepared.locked:
        _log_deferred(prepared, "lock_busy")
        return None
    if prepared.mode == "enforce" and prepared.non_decreasing:
        await db.flush()
        try:
            async with db.begin_nested():
                return await reconcile_workspace_slots(
                    db,
                    prepared.owner_user_id,
                    cause=prepared.cause,
                    allow_suspend=True,
                    change=prepared.change,
                    mode=prepared.mode,
                )
        except Exception as exc:  # noqa: BLE001 — a resume may wait for the sweep
            _log_deferred(prepared, "error", type(exc).__name__)
            return None
    try:
        return await reconcile_workspace_slots(
            db,
            prepared.owner_user_id,
            cause=prepared.cause,
            allow_suspend=True,
            change=prepared.change,
            mode=prepared.mode,
        )
    except Exception as exc:
        # The delivery fails as a whole; one line per owner to alert on.
        logger.warning(
            "workspace_slots_reconcile_failed",
            owner_user_id=prepared.owner_user_id,
            workspace_id=str(prepared.change.workspace_id),
            cause=prepared.cause,
            error_class=type(exc).__name__,
        )
        raise


async def reconcile_plan_change(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    owner_user_id: str,
    cause: SlotCause,
    previous_plan_name: str,
    previous_entitlement_source: str,
    new_plan_name: str,
    new_entitlement_source: str,
) -> ReconcileOutcome | None:
    """``prepare_plan_change`` then ``finish_plan_change`` (legacy push, admin plan)."""
    prepared = await prepare_plan_change(
        db,
        workspace_id=workspace_id,
        owner_user_id=owner_user_id,
        cause=cause,
        previous_plan_name=previous_plan_name,
        previous_entitlement_source=previous_entitlement_source,
        new_plan_name=new_plan_name,
        new_entitlement_source=new_entitlement_source,
    )
    return await finish_plan_change(db, prepared)


# ---------------------------------------------------------------------------
# Post-commit triggers (delete, transfers), transfer pre-check, erasure
# ---------------------------------------------------------------------------


async def reconcile_after_commit(
    db: AsyncSession, owner_user_ids: Iterable[str | None], *, cause: SlotCause
) -> None:
    """Resume and prune for each owner after the caller committed. Never raises.

    Each owner in a fresh transaction of its own (``isolated_session``) under
    the owner lock, tried with the short ``retry_nowait`` budget in
    ``enforce`` and once in ``shadow`` (this runs on the response path); no
    suspension. Waits on slot-table row locks are capped. A busy owner lock or
    row lock is logged as ``workspace_slots_deferred`` (``lock_busy``), any
    other error (a pool timeout included) the same way (``error``); the sweep
    finishes the work and the caller's committed change never turns into an
    error. No statement in mode ``off``.
    """
    mode = workspace_slot_mode()
    if mode == "off":
        return
    tries = 1 if mode == "shadow" else NOWAIT_LOCK_TRIES
    for owner in dict.fromkeys(o for o in owner_user_ids if o):
        try:
            async with isolated_session(db) as session:
                if not await owner_slot_lock(session, owner, wait=False, tries=tries):
                    logger.warning(
                        "workspace_slots_deferred",
                        owner_user_id=owner,
                        cause=cause,
                        reason="lock_busy",
                    )
                    continue
                await _bound_row_lock_waits(session)
                await reconcile_workspace_slots(
                    session, owner, cause=cause, allow_suspend=False, mode=mode
                )
                await session.commit()
        except DBAPIError as exc:
            if not is_lock_not_available(exc):
                logger.warning(
                    "workspace_slots_deferred",
                    owner_user_id=owner,
                    cause=cause,
                    reason="error",
                    error_class=type(exc).__name__,
                )
                continue
            logger.warning(
                "workspace_slots_deferred", owner_user_id=owner, cause=cause, reason="lock_busy"
            )
        except Exception as exc:  # noqa: BLE001 — never fail a committed change
            logger.warning(
                "workspace_slots_deferred",
                owner_user_id=owner,
                cause=cause,
                reason="error",
                error_class=type(exc).__name__,
            )


async def check_transfer_recipient(
    db: AsyncSession, *, workspace_id: UUID, recipient_user_id: str
) -> None:
    """Refuse a voluntary transfer unless the recipient has a free slot (``enforce``).

    Runs under the transferred workspace's row lock, before the owner flip:
    takes the RECIPIENT's owner lock without waiting (``retry_nowait``
    budget). A slot is free when the recipient's allowance after the
    transfer (cap and contracted count WITH the incoming workspace, so an
    incoming higher tier makes room) is above the number of workspaces they
    have active now. Their saved selection plays no part: the recipient's
    reconcile after the commit only resumes, so a workspace that could be
    active only by pushing an active one out would end one over the
    allowance. A contracted workspace into a full account is refused too.
    No statement unless ``enforce``.

    Raises:
        WorkspaceSlotsBusyError: The recipient's owner lock stayed busy (409).
        WorkspaceSlotTransferRefusedError: No free slot (409 ``SLOT-004``).
    """
    if workspace_slot_mode() != "enforce":
        return
    if not await owner_slot_lock(db, recipient_user_id, wait=False):
        raise WorkspaceSlotsBusyError()
    state = await _read_owner(db, recipient_user_id)
    incoming = (
        await db.execute(
            select(Workspace.plan_name, Workspace.entitlement_source, Workspace.created_at).where(
                Workspace.id == workspace_id
            )
        )
    ).one()
    current = [w for w in state.owned if w.id != workspace_id]
    after = [
        *current,
        OwnedWorkspace(
            id=workspace_id,
            plan_name=incoming.plan_name,
            entitlement_source=incoming.entitlement_source,
            created_at=incoming.created_at,
            suspended=True,
        ),
    ]
    allowance_after = slot_allowance(after, state.slot_bonus).allowance
    active_now = sum(1 for w in current if not w.suspended)
    if allowance_after <= active_now:
        logger.info(
            "workspace_transfer_refused_no_slot",
            workspace_id=str(workspace_id),
            recipient_user_id=recipient_user_id,
            allowance=allowance_after,
            active=active_now,
        )
        raise WorkspaceSlotTransferRefusedError()


async def slot_tables_present(db: AsyncSession) -> bool:
    """Whether both slot tables exist (catalog lookup only)."""
    return bool(
        (
            await db.execute(
                text(
                    "SELECT to_regclass('workspace_slot_states') IS NOT NULL "
                    "AND to_regclass('workspace_slot_suspensions') IS NOT NULL"
                )
            )
        ).scalar()
    )


async def delete_owner_slot_rows(db: AsyncSession, owner_user_id: str) -> dict[str, int]:
    """Delete ``owner_user_id``'s rows of both slot tables (erasure, user deletion).

    In every mode: rows written while the feature was on must not outlive the
    account. Guarded with ``to_regclass``, so it runs before the migration
    too (returns ``{}`` then). Locks the suspension rows in ``workspace_id``
    order. The caller commits.

    Returns:
        Deleted row counts per table (``{}`` when the tables do not exist).
    """
    if not await slot_tables_present(db):
        return {}
    suspensions = await _delete_suspensions(
        db, WorkspaceSlotSuspension.owner_user_id == owner_user_id
    )
    states = await db.execute(
        delete(WorkspaceSlotState).where(WorkspaceSlotState.owner_user_id == owner_user_id)
    )
    return {
        "workspace_slot_suspensions": suspensions,
        "workspace_slot_states": int(getattr(states, "rowcount", 0) or 0),
    }


async def assert_slot_tables_ready() -> None:
    """Startup check: ``shadow`` / ``enforce`` refuse to start without the tables.

    No database access in mode ``off``.

    Raises:
        RuntimeError: The mode is on and migration ``f56_2918_workspace_slots``
            has not run.
    """
    mode = workspace_slot_mode()
    if mode == "off":
        return
    from db.base import get_db

    async for db in get_db():
        present = await slot_tables_present(db)
    if not present:
        raise RuntimeError(
            f"WORKSPACE_SLOT_SUSPENSION={mode} needs the workspace slot tables "
            "(migration f56_2918_workspace_slots): run `alembic upgrade head` first, "
            "or set WORKSPACE_SLOT_SUSPENSION=off."
        )
    logger.info("workspace_slot_suspension_mode", mode=mode)


# ---------------------------------------------------------------------------
# Sweep (resume and prune only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    """One sweep run: owners visited, what changed, and where to continue."""

    owners: int = 0
    resumed: int = 0
    pruned: int = 0
    busy: int = 0
    failed: int = 0
    next_after: str = ""


SWEEP_BATCH = 200


async def sweep_workspace_slots(
    db: AsyncSession, *, after: str = "", until: str | None = None, limit: int = SWEEP_BATCH
) -> SweepOutcome:
    """Resume and prune for owners with suspension rows; never suspends.

    Visits at most ``limit`` owners with ``after < owner_user_id`` (and
    ``<= until`` when given) in id order, each in a transaction of its own
    after ONE try of the owner lock, with waits on slot-table row locks
    capped (a busy owner lock or row lock = skipped this run). ``next_after``
    is where the next batch continues (``""`` after the last owner). Finishes
    the resumes a deferred or lost post-commit reconcile left, and prunes rows
    of workspaces deleted or moved. No statement in mode ``off``.
    """
    mode = workspace_slot_mode()
    if mode == "off":
        return SweepOutcome()
    query = select(WorkspaceSlotSuspension.owner_user_id).where(
        WorkspaceSlotSuspension.owner_user_id > after
    )
    if until is not None:
        query = query.where(WorkspaceSlotSuspension.owner_user_id <= until)
    owners = list(
        (
            await db.execute(
                query.group_by(WorkspaceSlotSuspension.owner_user_id)
                .order_by(WorkspaceSlotSuspension.owner_user_id)
                .limit(limit)
            )
        ).scalars()
    )
    # Release this read's connection before the per-owner sessions.
    await db.commit()
    resumed = pruned = busy = failed = 0
    for owner in owners:
        try:
            async with isolated_session(db) as session:
                if not await owner_slot_lock(session, owner, wait=False, tries=1):
                    busy += 1
                    continue
                await _bound_row_lock_waits(session)
                outcome = await reconcile_workspace_slots(
                    session, owner, cause="sweep", allow_suspend=False, mode=mode
                )
                await session.commit()
                resumed += len(outcome.resumed)
                pruned += outcome.pruned
        except DBAPIError as exc:
            if not is_lock_not_available(exc):
                failed += 1
                logger.warning(
                    "workspace_slots_sweep_owner_failed",
                    owner_user_id=owner,
                    error_class=type(exc).__name__,
                )
                continue
            busy += 1  # a row lock held by another transaction: the next run
        except Exception as exc:  # noqa: BLE001 — one owner never stops the sweep
            failed += 1
            logger.warning(
                "workspace_slots_sweep_owner_failed",
                owner_user_id=owner,
                error_class=type(exc).__name__,
            )
    return SweepOutcome(
        owners=len(owners),
        resumed=resumed,
        pruned=pruned,
        busy=busy,
        failed=failed,
        next_after=owners[-1] if len(owners) == limit else "",
    )


# ---------------------------------------------------------------------------
# Account API: overview, preview, selection, and the end-to-end seed
# ---------------------------------------------------------------------------


def _allowance_view(allowance: SlotAllowance) -> SlotAllowanceView:
    summary = allowance.summary
    next_tier = next_tier_with_more_workspaces(summary.tier)
    return SlotAllowanceView(
        cap=summary.cap,
        base=summary.base,
        slot_bonus=summary.slot_bonus,
        tier_grant=summary.tier_grant,
        tier=summary.tier,
        next_tier=next_tier,
        cap_on_next_tier=cap_on_tier(summary, next_tier) if next_tier is not None else None,
    )


def _paid_plans_end_plan(
    state: _OwnerState, keep_ids: Sequence[UUID]
) -> tuple[SlotPlan, SlotAllowance]:
    """Every contracted workspace back on Free (admin-granted tiers keep theirs)."""
    ended = [
        dataclasses.replace(w, plan_name=str(PlanName.FREE)) if w.under_billing_contract else w
        for w in state.owned
    ]
    after = slot_allowance(ended, state.slot_bonus)
    before = slot_allowance(state.owned, state.slot_bonus)
    plan = plan_active_set(
        ended,
        after.summary.cap,
        drop=after.allowance < before.allowance,
        keep_ids=keep_ids,
        current_workspace_id=state.current_workspace_id,
    )
    return plan, after


def _overview(state: _OwnerState) -> SlotOverview:
    allowance = slot_allowance(state.owned, state.slot_bonus)
    ordered = sorted(state.owned, key=lambda w: (w.created_at, w.id))
    workspaces = tuple(
        SlotWorkspace(
            id=w.id,
            name=state.names[w.id],
            plan_name=w.plan_name,
            status="slot_suspended" if w.suspended else "active",
            suspended_at=state.own_rows.get(w.id) if w.suspended else None,
            under_billing_contract=w.under_billing_contract,
            is_current=w.id == state.current_workspace_id,
            created_at=w.created_at,
        )
        for w in ordered
    )
    active_count = sum(1 for w in state.owned if not w.suspended)
    if_paid_plans_end = None
    if allowance.contracted_count:
        plan, after = _paid_plans_end_plan(state, state.keep_ids)
        if_paid_plans_end = PaidPlansEndOutcome(
            cap=after.summary.cap, active=plan.active, suspended=plan.suspended
        )
    return SlotOverview(
        allowance=_allowance_view(allowance),
        owned_count=len(state.owned),
        active_count=active_count,
        active_slots=max(allowance.allowance, active_count),
        contracted_count=allowance.contracted_count,
        selection_required=(
            state.selection_required_since is not None and bool(state.suspended_now)
        ),
        selection=SlotSelection(
            workspace_ids=tuple(k for k in state.keep_ids if k in state.owned_ids),
            saved_at=state.selection_saved_at,
        ),
        version=state.version,
        if_paid_plans_end=if_paid_plans_end,
        workspaces=workspaces,
    )


def _validate_selection(workspace_ids: Sequence[UUID], owned_ids: frozenset[UUID]) -> None:
    seen: set[UUID] = set()
    invalid: list[str] = []
    for wid in workspace_ids:
        if (wid in seen or wid not in owned_ids) and str(wid) not in invalid:
            invalid.append(str(wid))
        seen.add(wid)
    if invalid:
        # One message whether the id exists or not: nothing about other
        # people's workspaces is disclosed.
        raise ValidationError(
            "Only workspaces you own can be selected, each once.",
            field="workspace_ids",
            invalid_ids=invalid,
        )


async def get_slot_overview(db: AsyncSession, owner_user_id: str) -> SlotOverview:
    """The account API's ``GET``: the owner's allowance, selection and workspaces.

    Read only (column selects, no lock). The route calls it only in mode
    ``enforce``. A user who owns nothing gets ``workspaces=()`` and the base
    allowance. ``workspaces`` is ordered by ``created_at``, then id; statuses
    follow the read-time rule (a contracted workspace is always active).
    """
    return _overview(await _read_owner(db, owner_user_id))


async def preview_selection(
    db: AsyncSession,
    owner_user_id: str,
    workspace_ids: Sequence[UUID],
    scenario: PreviewScenario,
) -> SelectionPreview:
    """What ``workspace_ids`` as the selection would make active. Saves nothing.

    ``current``: now, exactly what ``save_selection`` would apply (a swap, or
    a resume within the allowance); ``paid_plans_end``: after every
    subscription of the owner ended. ``active`` / ``suspended`` are the
    complete sets after applying, in priority order; ``changes`` only the
    differences from the active set now. No version check.

    Raises:
        ValidationError: An id is duplicated or not a live workspace the owner
            owns (422 ``VAL-001``, ``details.field = "workspace_ids"``,
            ``details.invalid_ids``; one message whether or not the id exists).
    """
    state = await _read_owner(db, owner_user_id)
    _validate_selection(workspace_ids, state.owned_ids)
    if scenario == "paid_plans_end":
        plan, allowance = _paid_plans_end_plan(state, workspace_ids)
    else:
        allowance = slot_allowance(state.owned, state.slot_bonus)
        plan = plan_active_set(
            state.owned,
            allowance.summary.cap,
            drop=False,
            keep_ids=workspace_ids,
            current_workspace_id=state.current_workspace_id,
        )
    suspended_now = state.suspended_now
    return SelectionPreview(
        scenario=scenario,
        cap=allowance.summary.cap,
        active_slots=plan.active_slots,
        active=plan.active,
        suspended=plan.suspended,
        unmet=plan.unmet,
        changes=SlotChanges(
            suspend=tuple(w for w in plan.suspended if w not in suspended_now),
            resume=tuple(w for w in plan.active if w in suspended_now),
        ),
    )


async def save_selection(
    db: AsyncSession,
    owner_user_id: str,
    workspace_ids: Sequence[UUID],
    expected_version: int,
) -> SlotOverview:
    """Store the owner's ordered selection and apply it at once; commit.

    Ends whatever transaction the session has open (the route's
    authentication only reads), then takes ``owner_slot_lock`` waiting as the
    first statement of a new one, so no row lock is ever held while it
    waits, then the owner's users row ``FOR KEY SHARE`` (the erasure fence).
    Then it validates, compares the version, stores the list (an empty list
    = the default order; it clears the prompt now but does not count as a
    choice at a later drop), clears ``selection_required_since`` and
    reconciles with ``cause="selection"``: a swap when something is
    suspended, else only stored. ``version`` moves only when the stored list
    or the active set changes, so a repeated save answers 200 with the same
    version. Commits, then returns the overview.

    Raises:
        ValidationError: As ``preview_selection`` (422 ``VAL-001``).
        WorkspaceSlotVersionConflictError: ``expected_version`` is not the
            current version (409 ``SLOT-003``).
        WorkspaceSlotsFullError: The selection names a suspended workspace and
            contracted workspaces hold every active slot (409 ``SLOT-002``).
        WorkspaceSlotsBusyError: The owner lock stayed busy for 5 s (409
            ``RES-002``, retryable).
        NotFoundException: The account is gone (erased meanwhile; 404).
    """
    if db.in_transaction():
        await db.commit()
    if not await owner_slot_lock(db, owner_user_id, wait=True):
        raise WorkspaceSlotsBusyError()
    # The account fence user-initiated writes take against erasure
    # (``services.erasure_write_guard``): the owner's users row FOR KEY SHARE,
    # after the owner lock (the order ``POST /workspaces`` uses). A save during
    # the erasure waits for it and then finds the account gone; no slot row is
    # written after the erasure deleted them.
    live = await db.scalar(
        select(User.id)
        .where(User.user_id == owner_user_id)
        .with_for_update(read=True, key_share=True)
    )
    if live is None:
        raise NotFoundException("User")
    state = await _read_owner(db, owner_user_id)
    _validate_selection(workspace_ids, state.owned_ids)
    if expected_version != state.version:
        raise WorkspaceSlotVersionConflictError(
            expected_version=expected_version, current_version=state.version
        )
    allowance = slot_allowance(state.owned, state.slot_bonus)
    suspended_now = state.suspended_now
    if any(wid in suspended_now for wid in workspace_ids):
        active_count = len(state.owned) - len(suspended_now)
        active_slots = max(allowance.allowance, active_count)
        if allowance.contracted_count >= active_slots:
            plan = plan_active_set(
                state.owned,
                allowance.summary.cap,
                drop=False,
                keep_ids=workspace_ids,
                current_workspace_id=state.current_workspace_id,
            )
            view = _allowance_view(allowance)
            raise WorkspaceSlotsFullError(
                cap=view.cap,
                active_slots=active_slots,
                active_count=active_count,
                contracted_count=allowance.contracted_count,
                next_tier=view.next_tier,
                cap_on_next_tier=view.cap_on_next_tier,
                unmet=[str(w) for w in plan.unmet],
            )
    now = utcnow()
    # An identical list (a repeated save, a retried request) keeps the version
    # and the saved time; the owner still acted, so the prompt is cleared.
    same = list(state.keep_ids) == list(workspace_ids)
    saved_at = state.selection_saved_at if same and state.selection_saved_at else now
    if (
        not same
        or saved_at != state.selection_saved_at
        or state.selection_required_since is not None
    ):
        await _write_state(
            db,
            owner_user_id,
            now=now,
            version=state.version if same else state.version + 1,
            selection_required_since=None,
            keep_ids=list(workspace_ids),
            selection_saved_at=saved_at,
        )
    await reconcile_workspace_slots(db, owner_user_id, cause="selection", allow_suspend=True)
    await db.commit()
    logger.info(
        "workspace_slot_selection_saved",
        owner_user_id=owner_user_id,
        selected=len(workspace_ids),
    )
    return _overview(await _read_owner(db, owner_user_id))


async def seed_default_suspension(
    db: AsyncSession,
    owner_user_id: str,
    *,
    now: datetime | None = None,
) -> SlotPlan:
    """Write the rows a drop to the owner's current allowance would write.

    For the end-to-end seed: the planner's default order (no selection) at
    the owner's current cap, as if every workspace had been active and the
    allowance had just dropped. Suspension rows get ``cause="billing_snapshot"``;
    the state row gets ``selection_required_since = now`` when anything is
    suspended. Idempotent (rows already in place are kept); mode-independent;
    the caller commits.

    Returns:
        The plan that was written.
    """
    now = now or utcnow()
    state = await _read_owner(db, owner_user_id)
    everything_active = [dataclasses.replace(w, suspended=False) for w in state.owned]
    allowance = slot_allowance(everything_active, state.slot_bonus)
    plan = plan_active_set(
        everything_active,
        allowance.summary.cap,
        drop=True,
        current_workspace_id=state.current_workspace_id,
    )
    if plan.suspended:
        await _insert_suspensions(
            db,
            owner_user_id,
            plan.suspended,
            cause="billing_snapshot",
            now=now,
            replace=False,
        )
        await db.execute(
            pg_insert(WorkspaceSlotState)
            .values(
                owner_user_id=owner_user_id,
                selection_required_since=now,
                version=1,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[WorkspaceSlotState.owner_user_id])
        )
    return plan
