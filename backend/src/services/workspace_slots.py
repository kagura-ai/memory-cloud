"""Workspace slot suspension: the planner and the transition (Issue #2918).

An owner's allowance is the #1550 cap (``utils.plan_resolver``: 1 + slot bonus
+ the grant of the highest tier they own) or, when more of their workspaces are
under a billing contract than that, the number of contracted workspaces: a
workspace somebody pays for is never suspended. When an event lowers the
allowance below the number of workspaces the owner has active (a subscription
ended, an admin lowered a plan or the slot bonus), the excess owned workspaces
are suspended, their data kept; whenever slots return they resume. Only owned
workspaces count; membership in somebody else's workspace takes no slot.

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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import and_, exists, not_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

import config.settings as _config_settings
from config.plan_tiers import PLAN_ORDER, PlanName, get_plan_tier, plan_rank
from models.auth import ENTITLEMENT_SOURCE_EXTERNAL_BILLING, Workspace
from models.workspace_slots import WorkspaceSlotState, WorkspaceSlotSuspension
from services.workspace_locks import NOWAIT_LOCK_TRIES
from utils.plan_resolver import WorkspaceCapSummary, resolve_workspace_cap

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
]

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
OWNER_SLOT_LOCK_PREFIX: Final = "workspace_create:"


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
    """True when the event lowered the owner's effective allowance. Pure.

    ``owned`` and ``slot_bonus`` are the state AFTER the event; the "before"
    side substitutes ``change``'s previous plan and entitlement source of the
    changed workspace (when the owner still owns it) or the previous bonus.
    Ending a workspace's contract alone is not a drop: an admin comp from a
    billed Basic to a granted Pro raises the allowance.
    """
    after = slot_allowance(owned, slot_bonus).allowance
    if change is None:
        return False
    before_owned = list(owned)
    if change.workspace_id is not None:
        before_owned = [
            OwnedWorkspace(
                id=w.id,
                plan_name=(
                    change.previous_plan_name
                    if change.previous_plan_name is not None
                    else w.plan_name
                ),
                entitlement_source=(
                    change.previous_entitlement_source
                    if change.previous_entitlement_source is not None
                    else w.entitlement_source
                ),
                created_at=w.created_at,
                suspended=w.suspended,
            )
            if w.id == change.workspace_id
            else w
            for w in owned
        ]
    before_bonus = (
        change.previous_slot_bonus if change.previous_slot_bonus is not None else slot_bonus
    )
    before = slot_allowance(before_owned, before_bonus).allowance
    return after < before


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
        drop: The event lowered the allowance (``is_allowance_drop``).
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
        drop: Whether the event lowered the allowance.
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
# Transition (owner lock, reconcile) and the account API's operations
# ---------------------------------------------------------------------------


def owner_slot_lock_key(owner_user_id: str) -> str:
    """The advisory-lock key of ``owner_user_id`` (``workspace_create:<owner>``)."""
    return f"{OWNER_SLOT_LOCK_PREFIX}{owner_user_id}"


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
    raise NotImplementedError("#2918 step 2")


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
        What changed.
    """
    raise NotImplementedError("#2918 step 2")


async def get_slot_overview(db: AsyncSession, owner_user_id: str) -> SlotOverview:
    """The account API's ``GET``: the owner's allowance, selection and workspaces.

    Read only (no lock). The route calls it only in mode ``enforce``. A user
    who owns nothing gets ``workspaces=()`` and the base allowance.
    """
    raise NotImplementedError("#2918 step 2")


async def preview_selection(
    db: AsyncSession,
    owner_user_id: str,
    workspace_ids: Sequence[UUID],
    scenario: PreviewScenario,
) -> SelectionPreview:
    """What ``workspace_ids`` as the selection would make active. Saves nothing.

    ``current``: now (a swap or a resume within the allowance);
    ``paid_plans_end``: after every subscription of the owner ended. No
    version check.

    Raises:
        ValidationError: An id is duplicated or not a live workspace the owner
            owns (422 ``VAL-001``, ``details.field = "workspace_ids"``,
            ``details.invalid_ids``; one message whether or not the id exists).
    """
    raise NotImplementedError("#2918 step 2")


async def save_selection(
    db: AsyncSession,
    owner_user_id: str,
    workspace_ids: Sequence[UUID],
    expected_version: int,
) -> SlotOverview:
    """Store the owner's ordered selection and apply it at once; commit.

    Takes ``owner_slot_lock`` waiting, as the first statement of the
    transaction (the caller must hold no row lock), then validates, compares
    the version, stores the list (an empty list = the default order), clears
    ``selection_required_since`` and reconciles with ``cause="selection"``
    (a swap when something is suspended). Commits, then returns the overview.

    Raises:
        ValidationError: As ``preview_selection`` (422 ``VAL-001``).
        WorkspaceSlotVersionConflictError: ``expected_version`` is not the
            current version (409 ``SLOT-003``).
        WorkspaceSlotsFullError: The selection names a suspended workspace and
            contracted workspaces hold every active slot (409 ``SLOT-002``).
        WorkspaceSlotsBusyError: The owner lock stayed busy for 5 s (409
            ``RES-002``, retryable).
    """
    raise NotImplementedError("#2918 step 2")


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
    raise NotImplementedError("#2918 step 2")
