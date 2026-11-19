"""The plan-change triggers of workspace slots on real PostgreSQL (#2918).

Ordered billing snapshot, legacy plan push, admin plan change, slot-bonus
PATCH: a drop suspends per the planner, other changes only resume, ``off``
touches nothing, ``shadow`` never suspends. The busy-lock and error table of
design 12.5 is driven with a second real session holding the owner lock:
a change that may lower the allowance answers the retryable 409 and commits
nothing; a non-decreasing one commits and defers; an unexpected error fails
the first and is deferred for the second.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import structlog
from fastapi import HTTPException
from sqlalchemy import delete, select, text

from api.routes.admin import update_workspace_slot_bonus
from api.routes.admin_plans import AdminUpdatePlanRequest, update_workspace_plan
from api.routes.internal_billing import BillingPlanPush, set_workspace_plan_from_billing
from models.auth import User, Workspace
from models.schemas import UpdateWorkspaceSlotBonusRequest
from models.workspace_billing_schemas import BILLING_V1_ADDON_KEYS, WorkspaceBillingPush
from models.workspace_slots import WorkspaceSlotSuspension
from services import workspace_slots as slots
from services.integration_entitlements import apply_integration_entitlements
from services.workspace_slots import (
    OwnedWorkspace,
    plan_active_set,
    slot_allowance,
    sweep_workspace_slots,
)
from utils.exceptions import WorkspaceSlotsBusyError

from ._slot_world import SlotWorld, StatementRecorder, slot_mode

pytestmark = pytest.mark.asyncio(loop_scope="session")

ADMIN = {"user_id": "slot-admin", "email": "admin@example.com", "role": "admin"}


# One validity window for the whole module: a replayed delivery must carry the
# same content as the original.
_WINDOW_FROM = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)


def _snapshot(revision: int, plan: str) -> WorkspaceBillingPush:
    now = _WINDOW_FROM + timedelta(days=1)
    return WorkspaceBillingPush.model_validate(
        {
            "revision": revision,
            "event_id": f"delivery-{revision}",
            "source_ref": "workspace-snapshot",
            "contract_version": "workspace-billing-snapshot-v1",
            "max_access": {
                "valid_from": (now - timedelta(days=1)).isoformat(),
                "valid_until": (now + timedelta(days=30)).isoformat(),
                "evidence_ref": "paid-base-term",
            },
            "services": [],
            "plan": {"plan_name": plan, "addons": dict.fromkeys(BILLING_V1_ADDON_KEYS, 0)},
        }
    )


async def _deliver(world: SlotWorld, wid: UUID, revision: int, plan: str):
    async with world.maker() as s:
        result = await apply_integration_entitlements(s, wid, _snapshot(revision, plan))
        await s.commit()
    return result


async def _owner_with_max(world: SlotWorld):
    """Max (delivered by billing, revision 1) + two Free; the first Free current."""
    owner = await world.user("trigger")
    maxed = await world.workspace(owner)
    free_a = await world.workspace(owner, current=True)
    free_b = await world.workspace(owner)
    with slot_mode("enforce"):
        await _deliver(world, maxed, 1, "promax")
    assert await world.plan_of(maxed) == ("promax", "external_billing")
    return owner, maxed, free_a, free_b


async def _final_plan(world: SlotWorld, owner: str):
    """The planner's result for the committed data, as a drop from everything active."""
    async with world.maker() as s:
        rows = (
            await s.execute(
                select(
                    Workspace.id,
                    Workspace.plan_name,
                    Workspace.entitlement_source,
                    Workspace.created_at,
                ).where(Workspace.owner_user_id == owner, Workspace.deleted_at.is_(None))
            )
        ).all()
        current = await s.scalar(select(User.current_workspace_id).where(User.user_id == owner))
    owned = [OwnedWorkspace(r.id, r.plan_name, r.entitlement_source, r.created_at) for r in rows]
    return plan_active_set(
        owned, slot_allowance(owned, 0).summary.cap, drop=True, current_workspace_id=current
    )


class TestOrderedSnapshot:
    async def test_a_drop_suspends_per_the_planner(self, slot_world: SlotWorld) -> None:
        owner, maxed, free_a, free_b = await _owner_with_max(slot_world)
        with slot_mode("enforce"):
            result = await _deliver(slot_world, maxed, 2, "free")
        assert result.outcome == "applied"
        assert await slot_world.rows(owner) == {maxed: owner, free_b: owner}
        assert (await slot_world.row_of(maxed)).cause == "billing_snapshot"
        assert (await slot_world.state(owner)).selection_required_since is not None

    async def test_replays_change_nothing(self, slot_world: SlotWorld) -> None:
        owner, maxed, free_a, free_b = await _owner_with_max(slot_world)
        with slot_mode("enforce"):
            await _deliver(slot_world, maxed, 3, "free")
            rows, state = await slot_world.rows(owner), await slot_world.state(owner)
            duplicate = await _deliver(slot_world, maxed, 3, "free")
            stale = await _deliver(slot_world, maxed, 2, "promax")
        assert (duplicate.outcome, stale.outcome) == ("duplicate", "stale")
        assert await slot_world.rows(owner) == rows
        assert (await slot_world.state(owner)).version == state.version
        assert await slot_world.plan_of(maxed) == ("free", "external_billing")

    async def test_an_upgrade_of_a_suspended_workspace_resumes_everything(
        self, slot_world: SlotWorld
    ) -> None:
        owner, maxed, free_a, free_b = await _owner_with_max(slot_world)
        with slot_mode("enforce"):
            await _deliver(slot_world, maxed, 2, "free")
            await _deliver(slot_world, free_b, 1, "promax")
        assert await slot_world.rows(owner) == {}
        assert await slot_world.plan_of(free_b) == ("promax", "external_billing")
        assert await slot_world.plan_of(maxed) == ("free", "external_billing")

    async def test_off_touches_no_slot_table_and_takes_no_owner_lock(
        self, slot_world: SlotWorld, async_engine
    ) -> None:
        owner, maxed, free_a, free_b = await _owner_with_max(slot_world)
        with slot_mode("off"), StatementRecorder(async_engine) as recorder:
            result = await _deliver(slot_world, maxed, 2, "free")
        assert result.outcome == "applied"
        assert recorder.touching_slot_tables() == []
        assert recorder.advisory_locks() == []
        assert await slot_world.rows(owner) == {}

    async def test_shadow_logs_and_never_suspends(self, slot_world: SlotWorld) -> None:
        owner, maxed, free_a, free_b = await _owner_with_max(slot_world)
        with slot_mode("shadow"), structlog.testing.capture_logs() as logs:
            result = await _deliver(slot_world, maxed, 2, "free")
        assert result.outcome == "applied"
        assert await slot_world.rows(owner) == {}
        would = [e for e in logs if e["event"] == "workspace_slots_would_change"]
        assert set(would[0]["would_suspend"]) == {str(maxed), str(free_b)}

    async def test_shadow_with_a_busy_lock_commits_after_one_try(
        self, slot_world: SlotWorld, async_engine
    ) -> None:
        owner, maxed, *_ = await _owner_with_max(slot_world)
        release = await slot_world.hold_owner_lock(owner)
        try:
            with (
                slot_mode("shadow"),
                structlog.testing.capture_logs() as logs,
                StatementRecorder(async_engine) as recorder,
            ):
                result = await _deliver(slot_world, maxed, 2, "free")
        finally:
            await release()
        assert result.outcome == "applied"
        # One try of the owner lock, no retry (no backoff between tries).
        assert len(recorder.advisory_locks()) == 1
        assert "pg_try_advisory_xact_lock" in recorder.advisory_locks()[0]
        assert [e["reason"] for e in logs if e["event"] == "workspace_slots_deferred"] == [
            "lock_busy"
        ]

    async def test_a_lowering_change_with_a_busy_lock_is_a_retryable_409(
        self, slot_world: SlotWorld
    ) -> None:
        owner, maxed, free_a, free_b = await _owner_with_max(slot_world)
        release = await slot_world.hold_owner_lock(owner)
        try:
            with slot_mode("enforce"), pytest.raises(WorkspaceSlotsBusyError) as busy:
                await _deliver(slot_world, maxed, 2, "free")
        finally:
            await release()
        assert (busy.value.status_code, busy.value.error_code) == (409, "RES-002")
        assert busy.value.details == {"reason": "workspace_slots_busy"}
        # Nothing was committed: plan, receipt and rows as before.
        assert await slot_world.plan_of(maxed) == ("promax", "external_billing")
        assert await slot_world.rows(owner) == {}
        # The retry succeeds.
        with slot_mode("enforce"):
            result = await _deliver(slot_world, maxed, 2, "free")
        assert result.outcome == "applied"
        assert await slot_world.rows(owner) == {maxed: owner, free_b: owner}

    async def test_a_non_decreasing_change_with_a_busy_lock_commits_and_defers(
        self, slot_world: SlotWorld
    ) -> None:
        owner, maxed, free_a, free_b = await _owner_with_max(slot_world)
        with slot_mode("enforce"):
            await _deliver(slot_world, maxed, 2, "free")
        release = await slot_world.hold_owner_lock(owner)
        try:
            with slot_mode("enforce"), structlog.testing.capture_logs() as logs:
                result = await _deliver(slot_world, free_b, 1, "promax")
        finally:
            await release()
        assert result.outcome == "applied"
        assert await slot_world.plan_of(free_b) == ("promax", "external_billing")
        deferred = [e for e in logs if e["event"] == "workspace_slots_deferred"]
        assert [e["reason"] for e in deferred] == ["lock_busy"]
        # The resume waits for the sweep.
        assert maxed in await slot_world.rows(owner)
        with slot_mode("enforce"):
            async with slot_world.maker() as s:
                outcome = await sweep_workspace_slots(s)
        assert outcome.resumed >= 1
        assert await slot_world.rows(owner) == {}

    async def test_a_non_decreasing_change_whose_reconcile_fails_commits_and_defers(
        self, slot_world: SlotWorld, monkeypatch
    ) -> None:
        owner, maxed, free_a, free_b = await _owner_with_max(slot_world)
        with slot_mode("enforce"):
            await _deliver(slot_world, maxed, 2, "free")

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(slots, "_reconcile", broken)
        with slot_mode("enforce"), structlog.testing.capture_logs() as logs:
            result = await _deliver(slot_world, free_b, 1, "promax")
        assert result.outcome == "applied"
        assert await slot_world.plan_of(free_b) == ("promax", "external_billing")
        assert [
            (e["reason"], e["error_class"])
            for e in logs
            if e["event"] == "workspace_slots_deferred"
        ] == [("error", "RuntimeError")]

    async def test_a_lowering_change_whose_reconcile_fails_fails_the_delivery(
        self, slot_world: SlotWorld, monkeypatch
    ) -> None:
        owner, maxed, *_ = await _owner_with_max(slot_world)

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(slots, "_reconcile", broken)
        with (
            slot_mode("enforce"),
            structlog.testing.capture_logs() as logs,
            pytest.raises(RuntimeError),
        ):
            await _deliver(slot_world, maxed, 2, "free")
        assert await slot_world.plan_of(maxed) == ("promax", "external_billing")
        # The alert the runbook names: one line per failed delivery, per owner.
        (failed,) = [e for e in logs if e["event"] == "workspace_slots_reconcile_failed"]
        assert failed["owner_user_id"] == owner
        assert failed["workspace_id"] == str(maxed)
        assert (failed["cause"], failed["error_class"]) == ("billing_snapshot", "RuntimeError")

    async def test_a_delivery_rolled_back_after_the_reconcile_leaves_no_rows(
        self, slot_world: SlotWorld
    ) -> None:
        owner, maxed, *_ = await _owner_with_max(slot_world)
        with slot_mode("enforce"):
            async with slot_world.maker() as s:
                await apply_integration_entitlements(s, maxed, _snapshot(2, "free"))
                assert await s.scalar(select(Workspace.plan_name).where(Workspace.id == maxed))
                await s.rollback()
        assert await slot_world.rows(owner) == {}
        assert await slot_world.state(owner) is None
        assert await slot_world.plan_of(maxed) == ("promax", "external_billing")

    @pytest.mark.parametrize("first", ["maxed", "pro"])
    async def test_sibling_deliveries_end_in_the_planners_result_in_either_order(
        self, slot_world: SlotWorld, first: str
    ) -> None:
        owner = await slot_world.user("siblings")
        maxed = await slot_world.workspace(owner)
        pro = await slot_world.workspace(owner)
        free_a = await slot_world.workspace(owner, current=True)
        await slot_world.workspace(owner)
        with slot_mode("enforce"):
            await _deliver(slot_world, maxed, 1, "promax")
            await _deliver(slot_world, pro, 1, "pro")
            order = [maxed, pro] if first == "maxed" else [pro, maxed]
            for wid in order:
                await _deliver(slot_world, wid, 2, "free")
        expected = await _final_plan(slot_world, owner)
        assert expected.active == (free_a,)
        assert set(await slot_world.rows(owner)) == set(expected.suspended)

    async def test_concurrent_sibling_deliveries_serialize_on_the_owner_lock(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("concurrent")
        maxed = await slot_world.workspace(owner)
        pro = await slot_world.workspace(owner)
        free_a = await slot_world.workspace(owner, current=True)
        await slot_world.workspace(owner)
        with slot_mode("enforce"):
            await _deliver(slot_world, maxed, 1, "promax")
            await _deliver(slot_world, pro, 1, "pro")
            results = await asyncio.gather(
                _deliver(slot_world, maxed, 2, "free"),
                _deliver(slot_world, pro, 2, "free"),
                return_exceptions=True,
            )
            # A refused one (busy beyond the budget) succeeds on retry.
            for wid, result in zip((maxed, pro), results, strict=True):
                if isinstance(result, WorkspaceSlotsBusyError):
                    await _deliver(slot_world, wid, 2, "free")
                else:
                    assert not isinstance(result, BaseException), result
        expected = await _final_plan(slot_world, owner)
        assert expected.active == (free_a,)
        assert set(await slot_world.rows(owner)) == set(expected.suspended)


async def _push(world: SlotWorld, wid: UUID, plan: str):
    async with world.maker() as s:
        return await set_workspace_plan_from_billing(
            str(wid), BillingPlanPush(plan_name=plan), None, s
        )


class TestLegacyPush:
    async def test_a_drop_suspends_and_a_same_plan_repush_is_not_a_drop(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("legacy")
        maxed = await slot_world.workspace(owner, plan="promax", source="external_billing")
        free_a = await slot_world.workspace(owner, current=True)
        free_b = await slot_world.workspace(owner)
        with slot_mode("enforce"):
            await _push(slot_world, maxed, "free")
            assert await slot_world.rows(owner) == {maxed: owner, free_b: owner}
            assert (await slot_world.row_of(free_b)).cause == "billing_plan"
            # The owner resumes one by hand (as a swap would); a re-push of
            # the same plan suspends nothing more.
            async with slot_world.maker() as s:
                await s.execute(
                    delete(WorkspaceSlotSuspension).where(
                        WorkspaceSlotSuspension.workspace_id == free_b
                    )
                )
                await s.commit()
            await _push(slot_world, maxed, "free")
        assert await slot_world.rows(owner) == {maxed: owner}
        assert free_a not in await slot_world.rows()

    async def test_a_free_push_to_a_soft_deleted_workspace_runs_no_slot_statement(
        self, slot_world: SlotWorld, async_engine
    ) -> None:
        owner = await slot_world.user("legacy-deleted")
        gone = await slot_world.workspace(owner, plan="pro", source="external_billing")
        await slot_world.workspace(owner)
        await slot_world.soft_delete(gone)
        with slot_mode("enforce"), StatementRecorder(async_engine) as recorder:
            await _push(slot_world, gone, "free")
        assert recorder.touching_slot_tables() == []

    async def test_a_lowering_push_with_a_busy_lock_is_a_retryable_409(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("legacy-busy")
        maxed = await slot_world.workspace(owner, plan="promax", source="external_billing")
        await slot_world.workspace(owner)
        release = await slot_world.hold_owner_lock(owner)
        try:
            with slot_mode("enforce"), pytest.raises(WorkspaceSlotsBusyError):
                await _push(slot_world, maxed, "free")
        finally:
            await release()
        assert await slot_world.plan_of(maxed) == ("promax", "external_billing")

    async def test_off_runs_no_slot_statement(self, slot_world: SlotWorld, async_engine) -> None:
        owner = await slot_world.user("legacy-off")
        maxed = await slot_world.workspace(owner, plan="promax", source="external_billing")
        await slot_world.workspace(owner)
        with slot_mode("off"), StatementRecorder(async_engine) as recorder:
            await _push(slot_world, maxed, "free")
        assert recorder.touching_slot_tables() == []
        assert await slot_world.plan_of(maxed) == ("free", "external_billing")


async def _admin_plan(world: SlotWorld, wid: UUID, plan: str):
    async with world.maker() as s:
        return await update_workspace_plan(
            wid, AdminUpdatePlanRequest(plan_name=plan, reason="test"), ADMIN, s
        )


class TestAdminPlan:
    async def test_a_comp_from_a_billed_plan_to_a_granted_higher_one_is_not_a_drop(
        self, slot_world: SlotWorld
    ) -> None:
        # Over the cap from before: a billed Basic and three Free, all active.
        owner = await slot_world.user("comp")
        basic = await slot_world.workspace(owner, plan="basic", source="external_billing")
        others = [await slot_world.workspace(owner) for _ in range(3)]
        with slot_mode("enforce"):
            await _admin_plan(slot_world, basic, "pro")
        assert await slot_world.plan_of(basic) == ("pro", "admin_grant")
        assert await slot_world.rows(owner) == {}
        rows = await slot_world.rows()
        assert all(w not in rows for w in others)

    async def test_an_admin_downgrade_is_a_drop(self, slot_world: SlotWorld) -> None:
        owner = await slot_world.user("admin-drop")
        pro = await slot_world.workspace(owner, plan="pro", source="admin_grant", current=True)
        free_a = await slot_world.workspace(owner)
        free_b = await slot_world.workspace(owner)
        with slot_mode("enforce"):
            await _admin_plan(slot_world, pro, "free")
        assert await slot_world.rows(owner) == {free_a: owner, free_b: owner}
        assert (await slot_world.row_of(free_a)).cause == "admin_plan"

    async def test_a_lowering_change_with_a_busy_lock_is_a_retryable_409(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("admin-busy")
        pro = await slot_world.workspace(owner, plan="pro", source="admin_grant")
        await slot_world.workspace(owner)
        await slot_world.workspace(owner)
        release = await slot_world.hold_owner_lock(owner)
        try:
            with slot_mode("enforce"), pytest.raises(WorkspaceSlotsBusyError):
                await _admin_plan(slot_world, pro, "free")
        finally:
            await release()
        assert await slot_world.plan_of(pro) == ("pro", "admin_grant")

    async def test_off_locks_and_flushes_the_row_and_touches_no_slot_table(
        self, slot_world: SlotWorld, async_engine
    ) -> None:
        owner = await slot_world.user("admin-off")
        pro = await slot_world.workspace(owner, plan="pro", source="external_billing")
        await slot_world.workspace(owner)
        with slot_mode("off"), StatementRecorder(async_engine) as recorder:
            await _admin_plan(slot_world, pro, "free")
        assert recorder.touching_slot_tables() == []
        # FOR NO KEY UPDATE (review C2): serializes with the billing writers'
        # FOR UPDATE without blocking foreign-key inserts of child rows.
        locking = [
            s for s in recorder.statements if s.startswith("SELECT") and "FOR NO KEY UPDATE" in s
        ]
        assert any("FROM workspaces" in s for s in locking), recorder.statements
        assert await slot_world.plan_of(pro) == ("free", "admin_grant")


async def _bonus(world: SlotWorld, owner: str, delta: int):
    async with world.maker() as s:
        return await update_workspace_slot_bonus(
            owner,
            UpdateWorkspaceSlotBonusRequest(delta=delta, reason="test"),
            ADMIN,
            s,
        )


class TestSlotBonus:
    async def test_a_reduction_suspends_and_an_increase_resumes(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("bonus", bonus=2)
        current = await slot_world.workspace(owner, current=True)
        second = await slot_world.workspace(owner)
        third = await slot_world.workspace(owner)
        with slot_mode("enforce"):
            await _bonus(slot_world, owner, -2)
            assert await slot_world.rows(owner) == {second: owner, third: owner}
            assert (await slot_world.row_of(second)).cause == "slot_bonus"
            await _bonus(slot_world, owner, 1)
            assert await slot_world.rows(owner) == {third: owner}
            await _bonus(slot_world, owner, 1)
        assert await slot_world.rows(owner) == {}
        assert current not in await slot_world.rows()

    async def test_off_runs_no_slot_statement(self, slot_world: SlotWorld, async_engine) -> None:
        owner = await slot_world.user("bonus-off", bonus=2)
        for _ in range(3):
            await slot_world.workspace(owner)
        with slot_mode("off"), StatementRecorder(async_engine) as recorder:
            await _bonus(slot_world, owner, -2)
        assert recorder.touching_slot_tables() == []
        assert await slot_world.rows(owner) == {}

    async def test_a_bonus_change_waits_for_the_owner_lock(self, slot_world: SlotWorld) -> None:
        owner = await slot_world.user("bonus-wait", bonus=1)
        await slot_world.workspace(owner)
        await slot_world.workspace(owner)
        release = await slot_world.hold_owner_lock(owner)
        with slot_mode("enforce"):
            task = asyncio.create_task(_bonus(slot_world, owner, -1))
            await asyncio.sleep(0.3)
            assert not task.done()
            await release()
            await asyncio.wait_for(task, 10)
        assert len(await slot_world.rows(owner)) == 1


class TestChangedWorkspaceHoldingARow:
    """Review A1: a suspended workspace that gets a paid plan with no extra
    slot swaps out an active one; it is not counted as active already."""

    async def test_a_suspended_free_workspace_bought_as_basic_swaps_and_its_end_keeps_one(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("swap")
        kept = await slot_world.workspace(owner, current=True)
        bought = await slot_world.workspace(owner)
        await slot_world.suspend_row(bought, owner)
        with slot_mode("enforce"):
            await _deliver(slot_world, bought, 1, "basic")
            assert await slot_world.rows(owner) == {kept: owner}
            await _deliver(slot_world, bought, 2, "free")
        assert await slot_world.rows(owner) == {kept: owner}
        assert await slot_world.plan_of(bought) == ("free", "external_billing")

    async def test_a_purchase_deferred_by_a_busy_lock_ends_with_one_active(
        self, slot_world: SlotWorld
    ) -> None:
        """Allowance 1. The purchase is deferred (busy lock), the sweep only
        resumes, so both are active; the end of that contract keeps the
        allowance at 1 and cuts the excess."""
        owner = await slot_world.user("deferred")
        kept = await slot_world.workspace(owner, current=True)
        bought = await slot_world.workspace(owner)
        await slot_world.suspend_row(bought, owner)
        release = await slot_world.hold_owner_lock(owner)
        try:
            with slot_mode("enforce"):
                await _deliver(slot_world, bought, 1, "basic")
        finally:
            await release()
        with slot_mode("enforce"):
            async with slot_world.maker() as s:
                await sweep_workspace_slots(s)
            assert await slot_world.rows(owner) == {}
            await _deliver(slot_world, bought, 2, "free")
        assert await slot_world.rows(owner) == {bought: owner}
        assert kept not in await slot_world.rows(owner)

    async def test_an_owner_over_the_allowance_is_cut_when_a_paid_contract_ends(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("over")
        await slot_world.workspace(owner, current=True)
        others = [await slot_world.workspace(owner) for _ in range(2)]
        billed = await slot_world.workspace(owner)
        with slot_mode("enforce"):
            await _deliver(slot_world, billed, 1, "basic")
            assert await slot_world.rows(owner) == {}  # a purchase never suspends
            await _deliver(slot_world, billed, 2, "free")
        assert set(await slot_world.rows(owner)) == {*others, billed}

    @pytest.mark.parametrize("first", ["cancel", "buy"])
    async def test_a_cancellation_and_a_basic_purchase_end_alike_in_either_order(
        self, slot_world: SlotWorld, first: str
    ) -> None:
        owner = await slot_world.user("mixed")
        pro = await slot_world.workspace(owner, current=True)
        free_a = await slot_world.workspace(owner)
        free_b = await slot_world.workspace(owner)
        with slot_mode("enforce"):
            await _deliver(slot_world, pro, 1, "pro")
            events = [(pro, 2, "free"), (free_a, 1, "basic")]
            for wid, revision, plan in events if first == "cancel" else reversed(events):
                await _deliver(slot_world, wid, revision, plan)
        assert set(await slot_world.rows(owner)) == {pro, free_b}


def _failing_inside_the_savepoint(monkeypatch) -> None:
    """Make the reconcile run a failing SQL statement (a database error, not a
    Python one): without its savepoint the caller's transaction would be
    aborted and could not commit."""
    real = slots._read_owner

    async def failing(db, owner_user_id):
        state = await real(db, owner_user_id)
        await db.execute(text("SELECT 1 / 0"))
        return state

    monkeypatch.setattr(slots, "_read_owner", failing)


class TestSavepointsHoldADatabaseError:
    """Review D: the savepoint branches with an error raised by the database."""

    async def test_a_non_decreasing_snapshot_commits_after_a_database_error(
        self, slot_world: SlotWorld, monkeypatch
    ) -> None:
        owner, maxed, free_a, free_b = await _owner_with_max(slot_world)
        with slot_mode("enforce"):
            await _deliver(slot_world, maxed, 2, "free")
        _failing_inside_the_savepoint(monkeypatch)
        with slot_mode("enforce"), structlog.testing.capture_logs() as logs:
            async with slot_world.maker() as s:
                await apply_integration_entitlements(s, free_b, _snapshot(1, "promax"))
                assert await s.scalar(text("SELECT 1")) == 1  # still usable
                await s.commit()
        assert await slot_world.plan_of(free_b) == ("promax", "external_billing")
        assert [e["reason"] for e in logs if e["event"] == "workspace_slots_deferred"] == ["error"]

    async def test_shadow_commits_after_a_database_error(
        self, slot_world: SlotWorld, monkeypatch
    ) -> None:
        owner, maxed, *_ = await _owner_with_max(slot_world)
        _failing_inside_the_savepoint(monkeypatch)
        with slot_mode("shadow"), structlog.testing.capture_logs() as logs:
            async with slot_world.maker() as s:
                await apply_integration_entitlements(s, maxed, _snapshot(2, "free"))
                assert await s.scalar(text("SELECT 1")) == 1
                await s.commit()
        assert await slot_world.plan_of(maxed) == ("free", "external_billing")
        assert [e["event"] for e in logs if e["event"].startswith("workspace_slots")] == [
            "workspace_slots_shadow_failed"
        ]

    async def test_shadow_does_not_wait_on_a_locked_row(
        self, slot_world: SlotWorld, async_engine
    ) -> None:
        """A shadow delete that meets a locked row gives up (``lock_timeout``)
        instead of waiting for the holder, and logs it."""
        owner = await slot_world.user("shadow-locked", bonus=1)
        await slot_world.workspace(owner, current=True)
        waiting = await slot_world.workspace(owner)
        await slot_world.suspend_row(waiting, owner)
        holder = slot_world.maker()
        await holder.execute(
            text("SELECT 1 FROM workspace_slot_suspensions WHERE workspace_id = :w FOR UPDATE"),
            {"w": waiting},
        )
        try:
            with slot_mode("shadow"), structlog.testing.capture_logs() as logs:
                async with slot_world.maker() as s:
                    assert await slots.owner_slot_lock(s, owner, wait=False, tries=1)
                    # The holder never lets go before the finally below: a
                    # reconcile that waited for it would never return.
                    await asyncio.wait_for(
                        slots.reconcile_workspace_slots(
                            s, owner, cause="sweep", allow_suspend=False
                        ),
                        30,
                    )
                    assert await s.scalar(text("SELECT current_setting('lock_timeout')")) == "0"
                    await s.commit()
        finally:
            await holder.rollback()
            await holder.close()
        assert [e["event"] for e in logs] == ["workspace_slots_shadow_failed"]
        assert await slot_world.rows(owner) == {waiting: owner}


class TestLockBeforeTheSyncStop:
    async def test_the_owner_lock_is_tried_before_the_sync_stop(
        self, slot_world: SlotWorld, monkeypatch
    ) -> None:
        """Review D: only the workspace row is held while the try-lock backs off."""
        import services.sync_management as sync_management

        owner, maxed, *_ = await _owner_with_max(slot_world)
        order: list[str] = []
        real_lock, real_stop = slots.owner_slot_lock, sync_management.stop_ineligible_workspace

        async def lock(*args, **kwargs):
            order.append("owner_lock")
            return await real_lock(*args, **kwargs)

        async def stop(*args, **kwargs):
            order.append("sync_stop")
            return await real_stop(*args, **kwargs)

        monkeypatch.setattr(slots, "owner_slot_lock", lock)
        monkeypatch.setattr(sync_management, "stop_ineligible_workspace", stop)
        with slot_mode("enforce"):
            await _deliver(slot_world, maxed, 2, "free")
        assert order == ["owner_lock", "sync_stop"]


class TestLegacyAndAdminBranches:
    """Review D: the busy and error branches of the legacy push and the admin
    plan route, as the snapshot tests drive them."""

    @pytest.mark.parametrize("route", ["legacy", "admin"])
    async def test_a_non_decreasing_change_with_a_busy_lock_commits_and_defers(
        self, slot_world: SlotWorld, route: str
    ) -> None:
        owner = await slot_world.user(f"{route}-up")
        free = await slot_world.workspace(owner, current=True)
        await slot_world.workspace(owner)
        release = await slot_world.hold_owner_lock(owner)
        try:
            with slot_mode("enforce"), structlog.testing.capture_logs() as logs:
                if route == "legacy":
                    await _push(slot_world, free, "basic")
                else:
                    await _admin_plan(slot_world, free, "pro")
        finally:
            await release()
        expected = ("basic", "external_billing") if route == "legacy" else ("pro", "admin_grant")
        assert await slot_world.plan_of(free) == expected
        assert [e["reason"] for e in logs if e["event"] == "workspace_slots_deferred"] == [
            "lock_busy"
        ]

    @pytest.mark.parametrize("route", ["legacy", "admin"])
    async def test_a_non_decreasing_change_whose_reconcile_fails_commits_and_defers(
        self, slot_world: SlotWorld, route: str, monkeypatch
    ) -> None:
        owner = await slot_world.user(f"{route}-up-error")
        free = await slot_world.workspace(owner, current=True)
        _failing_inside_the_savepoint(monkeypatch)
        with slot_mode("enforce"), structlog.testing.capture_logs() as logs:
            if route == "legacy":
                await _push(slot_world, free, "basic")
            else:
                await _admin_plan(slot_world, free, "pro")
        expected = ("basic", "external_billing") if route == "legacy" else ("pro", "admin_grant")
        assert await slot_world.plan_of(free) == expected
        assert [e["reason"] for e in logs if e["event"] == "workspace_slots_deferred"] == ["error"]

    @pytest.mark.parametrize("route", ["legacy", "admin"])
    async def test_a_lowering_change_whose_reconcile_fails_fails_the_request(
        self, slot_world: SlotWorld, route: str, monkeypatch
    ) -> None:
        owner = await slot_world.user(f"{route}-down-error")
        source = "external_billing" if route == "legacy" else "admin_grant"
        pro = await slot_world.workspace(owner, plan="pro", source=source, current=True)
        await slot_world.workspace(owner)

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(slots, "_reconcile", broken)
        with slot_mode("enforce"), pytest.raises((RuntimeError, HTTPException)):
            if route == "legacy":
                await _push(slot_world, pro, "free")
            else:
                await _admin_plan(slot_world, pro, "free")
        assert await slot_world.plan_of(pro) == ("pro", source)
