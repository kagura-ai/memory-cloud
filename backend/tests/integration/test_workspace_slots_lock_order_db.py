"""The workspace slot paths take the global lock order (#2918, design 3 / 12.5).

Order: the changed workspace row, then the owner slot lock (tried, never
waited on, while a row is held), then the slot-table rows. Reconcile takes no
workspace or ``users`` row lock. Paths that wait on the owner lock (the
selection PUT, the post-commit reconciles, the slot-bonus PATCH) take it
before any row lock of their transaction. Each path below runs in
``enforce`` under the statement trace of ``test_erasure_lock_order_db``.
"""

from __future__ import annotations

import pytest

from api.routes.admin import update_workspace_slot_bonus
from api.routes.admin_plans import AdminUpdatePlanRequest, update_workspace_plan
from api.routes.internal_billing import BillingPlanPush, set_workspace_plan_from_billing
from models.schemas import UpdateWorkspaceSlotBonusRequest
from services.integration_entitlements import apply_integration_entitlements
from services.workspace_ownership_service import WorkspaceOwnershipService
from services.workspace_service import WorkspaceService
from services.workspace_slots import save_selection, sweep_workspace_slots

from ._slot_world import SlotWorld, slot_mode
from .test_erasure_lock_order_db import _assert_global_order, _LockTrace
from .test_workspace_slots_triggers_db import ADMIN, _snapshot

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _transactions_with_owner_lock(trace: _LockTrace) -> list[list[tuple[str, bool]]]:
    return [tx for tx in trace.kinds() if any(kind == "owner_slot" for kind, _ in tx)]


def _before(tx: list[tuple[str, bool]], first: str, then: str) -> bool:
    kinds = [kind for kind, _ in tx]
    return kinds.index(first) < kinds.index(then)


async def _owner(world: SlotWorld, prefix: str):
    owner = await world.user(prefix)
    paid = await world.workspace(owner, plan="promax", source="external_billing")
    keep = await world.workspace(owner, current=True)
    other = await world.workspace(owner)
    return owner, paid, keep, other


async def test_the_snapshot_drop_holds_the_row_then_tries_the_owner_lock(
    slot_world: SlotWorld, async_engine
) -> None:
    owner, paid, keep, other = await _owner(slot_world, "order-snapshot")
    with slot_mode("enforce"):
        async with slot_world.maker() as s:
            await apply_integration_entitlements(s, paid, _snapshot(1, "promax"))
            await s.commit()
        with _LockTrace(async_engine) as trace:
            async with slot_world.maker() as s:
                await apply_integration_entitlements(s, paid, _snapshot(2, "free"))
                await s.commit()
    _assert_global_order(trace)
    [tx] = _transactions_with_owner_lock(trace)
    assert ("owner_slot", True) in tx
    assert _before(tx, "ws_live", "owner_slot")
    assert await slot_world.rows(owner) == {paid: owner, other: owner}


async def test_the_legacy_push_and_the_admin_plan_route_take_the_same_order(
    slot_world: SlotWorld, async_engine
) -> None:
    owner, paid, keep, other = await _owner(slot_world, "order-plan")
    with slot_mode("enforce"), _LockTrace(async_engine) as trace:
        async with slot_world.maker() as s:
            await set_workspace_plan_from_billing(
                str(paid), BillingPlanPush(plan_name="free"), None, s
            )
        async with slot_world.maker() as s:
            await update_workspace_plan(
                keep, AdminUpdatePlanRequest(plan_name="pro", reason="comp"), ADMIN, s
            )
    _assert_global_order(trace)
    owner_txs = _transactions_with_owner_lock(trace)
    assert len(owner_txs) == 2
    for tx in owner_txs:
        assert ("owner_slot", True) in tx
        assert _before(tx, "ws_live", "owner_slot")


async def test_the_waiting_paths_take_the_owner_lock_first(
    slot_world: SlotWorld, async_engine
) -> None:
    owner, paid, keep, other = await _owner(slot_world, "order-wait")
    member = await slot_world.user("order-member")
    shared = await slot_world.workspace(owner, members={member: "admin"})
    with slot_mode("enforce"), _LockTrace(async_engine) as trace:
        async with slot_world.maker() as s:
            await update_workspace_slot_bonus(
                owner, UpdateWorkspaceSlotBonusRequest(delta=1, reason="test"), ADMIN, s
            )
        async with slot_world.maker() as s:
            await save_selection(s, owner, [other], 0)
        async with slot_world.maker() as s:
            await WorkspaceService(s).delete_workspace(other, deleted_by=owner)
        async with slot_world.maker() as s:
            await WorkspaceOwnershipService(s).transfer_ownership(
                workspace_id=shared,
                current_owner_id=owner,
                target_user_id=member,
                performed_by_email="owner@example.com",
            )
        async with slot_world.maker() as s:
            await sweep_workspace_slots(s)
    _assert_global_order(trace)
    owner_txs = _transactions_with_owner_lock(trace)
    waited = [tx for tx in owner_txs if ("owner_slot", False) in tx]
    # bonus PATCH, selection PUT, delete (one owner), transfer (two owners)
    assert len(waited) >= 5
    for tx in waited:
        assert tx[0] == ("owner_slot", False), tx
    # The transfer's recipient check tries the lock under its rows.
    tried = [tx for tx in owner_txs if ("owner_slot", True) in tx and ("ws_live", True) in tx]
    assert tried, owner_txs
