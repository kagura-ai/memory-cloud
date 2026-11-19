"""Workspace slot suspension on real PostgreSQL (#2918).

The read-time rule: a suspension row counts only while the owner it was
stamped with still owns the live workspace and the workspace is not under a
billing contract. Rows left behind by an ownership move, a deletion or a mode
switch are inert; a contracted workspace is never reported suspended.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Workspace
from models.workspace_slots import WorkspaceSlotState, WorkspaceSlotSuspension
from services import workspace_slots as slots
from services.workspace_slots import (
    SlotChange,
    owner_selection_required,
    owner_slot_lock,
    reconcile_workspace_slots,
    seed_default_suspension,
    suspended_workspace_ids,
)

from ._slot_world import SlotWorld, StatementRecorder, slot_mode

pytestmark = pytest.mark.asyncio(loop_scope="session")

T0 = datetime(2031, 5, 1, 12, 0, 0)


def _mode(mode: str):
    return patch(
        "config.settings.get_settings",
        return_value=SimpleNamespace(workspace_slot_suspension=mode),
    )


async def _workspace(
    db: AsyncSession,
    owner: str,
    *,
    plan: str = "free",
    source: str = "admin_grant",
    deleted: bool = False,
) -> UUID:
    wid = uuid4()
    db.add(
        Workspace(
            id=wid,
            name=f"slots-{wid.hex[:8]}",
            owner_user_id=owner,
            plan_name=plan,
            entitlement_source=source,
            deleted_at=T0 if deleted else None,
        )
    )
    await db.flush()
    return wid


def _suspend(db: AsyncSession, wid: UUID, owner: str) -> None:
    db.add(
        WorkspaceSlotSuspension(
            workspace_id=wid, owner_user_id=owner, cause="billing_snapshot", suspended_at=T0
        )
    )


class TestReadTimeRule:
    async def test_only_rows_of_the_current_owner_on_live_uncontracted_workspaces_count(
        self, db_session: AsyncSession
    ) -> None:
        owner = f"slot-owner-{uuid4().hex[:8]}"
        other = f"slot-other-{uuid4().hex[:8]}"
        suspended = await _workspace(db_session, owner)
        active = await _workspace(db_session, owner)
        moved = await _workspace(db_session, owner)  # row stamped for the previous owner
        deleted = await _workspace(db_session, owner, deleted=True)
        contracted = await _workspace(db_session, owner, plan="pro", source="external_billing")
        granted = await _workspace(db_session, owner, plan="pro", source="admin_grant")
        _suspend(db_session, suspended, owner)
        _suspend(db_session, moved, other)
        _suspend(db_session, deleted, owner)
        _suspend(db_session, contracted, owner)
        _suspend(db_session, granted, owner)
        await db_session.flush()

        ids = [suspended, active, moved, deleted, contracted, granted]
        with _mode("enforce"):
            assert await suspended_workspace_ids(db_session, ids) == {suspended, granted}
        for mode in ("off", "shadow"):
            with _mode(mode):
                assert await suspended_workspace_ids(db_session, ids) == frozenset()

    async def test_selection_required_needs_the_flag_and_a_suspended_workspace(
        self, db_session: AsyncSession
    ) -> None:
        owner = f"slot-owner-{uuid4().hex[:8]}"
        wid = await _workspace(db_session, owner)
        with _mode("enforce"):
            assert await owner_selection_required(db_session, owner) is False

            db_session.add(
                WorkspaceSlotState(owner_user_id=owner, selection_required_since=T0, updated_at=T0)
            )
            await db_session.flush()
            # Flag set, nothing suspended.
            assert await owner_selection_required(db_session, owner) is False

            _suspend(db_session, wid, owner)
            await db_session.flush()
            assert await owner_selection_required(db_session, owner) is True

        with _mode("shadow"):
            assert await owner_selection_required(db_session, owner) is False

        state = await db_session.get(WorkspaceSlotState, owner)
        assert state is not None
        assert (state.keep_workspace_ids, state.version) == ([], 0)
        state.selection_required_since = None
        await db_session.flush()
        with _mode("enforce"):
            assert await owner_selection_required(db_session, owner) is False


# ---------------------------------------------------------------------------
# The owner lock and the transition (reconcile_workspace_slots)
# ---------------------------------------------------------------------------


async def _reconcile(world: SlotWorld, owner: str, **kwargs):
    async with world.maker() as s:
        assert await owner_slot_lock(s, owner, wait=True)
        outcome = await reconcile_workspace_slots(s, owner, **kwargs)
        await s.commit()
    return outcome


async def _issue_owner(world: SlotWorld) -> tuple[str, UUID, UUID, UUID]:
    """The issue's owner: one Max workspace (billed) and two Free, the first
    Free one current."""
    owner = await world.user("issue")
    maxed = await world.workspace(owner, plan="promax", source="external_billing")
    free_a = await world.workspace(owner, current=True)
    free_b = await world.workspace(owner)
    return owner, maxed, free_a, free_b


class TestOwnerLock:
    async def test_try_lock_gives_up_after_the_budget_and_the_transaction_stays_usable(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user()
        release = await slot_world.hold_owner_lock(owner)
        try:
            async with slot_world.maker() as s:
                started = asyncio.get_running_loop().time()
                assert await owner_slot_lock(s, owner, wait=False) is False
                elapsed = asyncio.get_running_loop().time() - started
                assert 0.5 < elapsed < 3
                assert await s.scalar(text("SELECT 1")) == 1
                assert await owner_slot_lock(s, owner, wait=False, tries=1) is False
        finally:
            await release()
        async with slot_world.maker() as s:
            assert await owner_slot_lock(s, owner, wait=False, tries=1) is True
            # Re-entrant inside the transaction that holds it.
            assert await owner_slot_lock(s, owner, wait=False, tries=1) is True
            await s.rollback()

    async def test_waiting_lock_times_out_restores_lock_timeout_and_stays_usable(
        self, slot_world: SlotWorld, monkeypatch
    ) -> None:
        monkeypatch.setattr(slots, "_WAIT_LOCK_TIMEOUT", "300ms")
        owner = await slot_world.user()
        release = await slot_world.hold_owner_lock(owner)
        try:
            async with slot_world.maker() as s:
                await s.execute(text("SET LOCAL lock_timeout = '7s'"))
                assert await owner_slot_lock(s, owner, wait=True) is False
                assert await s.scalar(text("SELECT current_setting('lock_timeout')")) == "7s"
        finally:
            await release()
        async with slot_world.maker() as s:
            await s.execute(text("SET LOCAL lock_timeout = '7s'"))
            assert await owner_slot_lock(s, owner, wait=True) is True
            assert await s.scalar(text("SELECT current_setting('lock_timeout')")) == "7s"
            await s.rollback()

    async def test_waiting_lock_is_obtained_when_the_holder_commits(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user()
        release = await slot_world.hold_owner_lock(owner)
        async with slot_world.maker() as s:
            waiter = asyncio.create_task(owner_slot_lock(s, owner, wait=True))
            await asyncio.sleep(0.3)
            assert not waiter.done()
            await release()
            assert await asyncio.wait_for(waiter, 5) is True
            await s.rollback()


class TestReconcile:
    async def test_off_runs_no_statement(self, slot_world: SlotWorld, async_engine) -> None:
        owner, *_ = await _issue_owner(slot_world)
        with slot_mode("off"), StatementRecorder(async_engine) as recorder:
            async with slot_world.maker() as s:
                outcome = await reconcile_workspace_slots(
                    s, owner, cause="billing_snapshot", allow_suspend=True
                )
        assert outcome.mode == "off"
        assert recorder.statements == []

    async def test_drop_suspends_per_the_planner_and_requires_a_selection(
        self, slot_world: SlotWorld
    ) -> None:
        owner, maxed, free_a, free_b = await _issue_owner(slot_world)
        await slot_world.set_plan(maxed, "free", "external_billing")
        with slot_mode("enforce"), structlog.testing.capture_logs() as logs:
            outcome = await _reconcile(
                slot_world,
                owner,
                cause="billing_snapshot",
                allow_suspend=True,
                change=SlotChange(maxed, "promax", "external_billing"),
            )
        assert outcome.drop is True
        # The default keeps the owner's current workspace.
        assert set(outcome.suspended) == {maxed, free_b}
        assert await slot_world.rows(owner) == {maxed: owner, free_b: owner}
        row = await slot_world.row_of(maxed)
        assert (row.cause, row.reason) == ("billing_snapshot", "allowance")
        state = await slot_world.state(owner)
        assert state.selection_required_since is not None
        assert state.version == 1
        assert [e["event"] for e in logs if e["event"].startswith("workspace_slots")] == [
            "workspace_slots_reconciled"
        ]

    async def test_a_non_drop_only_resumes(self, slot_world: SlotWorld) -> None:
        owner, maxed, free_a, free_b = await _issue_owner(slot_world)
        await slot_world.set_plan(maxed, "free", "external_billing")
        with slot_mode("enforce"):
            # The same lowered state without the event's previous values is
            # not a drop: an owner already over the cap stays untouched.
            outcome = await _reconcile(slot_world, owner, cause="sweep", allow_suspend=True)
            assert (outcome.drop, outcome.suspended) == (False, ())
            # A drop on a resume-only path suspends nothing either.
            outcome = await _reconcile(
                slot_world,
                owner,
                cause="workspace_delete",
                allow_suspend=False,
                change=SlotChange(maxed, "promax", "external_billing"),
            )
            assert (outcome.drop, outcome.suspended) == (False, ())
        assert await slot_world.rows(owner) == {}

    async def test_an_upgrade_resumes_everything_and_clears_the_flag(
        self, slot_world: SlotWorld
    ) -> None:
        owner, maxed, free_a, free_b = await _issue_owner(slot_world)
        await slot_world.set_plan(maxed, "free", "external_billing")
        with slot_mode("enforce"):
            await _reconcile(
                slot_world,
                owner,
                cause="billing_snapshot",
                allow_suspend=True,
                change=SlotChange(maxed, "promax", "external_billing"),
            )
            await slot_world.set_plan(free_b, "promax", "external_billing")
            outcome = await _reconcile(
                slot_world,
                owner,
                cause="billing_snapshot",
                allow_suspend=True,
                change=SlotChange(free_b, "free", "admin_grant"),
            )
        assert outcome.drop is False
        # The upgraded one still held its row: it counts as resumed too.
        assert set(outcome.resumed) == {maxed, free_b}
        assert await slot_world.rows(owner) == {}
        state = await slot_world.state(owner)
        assert (state.selection_required_since, state.version) == (None, 2)
        # Each keeps its own plan.
        assert await slot_world.plan_of(maxed) == ("free", "external_billing")
        assert await slot_world.plan_of(free_b) == ("promax", "external_billing")

    async def test_shadow_logs_never_inserts_and_still_deletes(self, slot_world: SlotWorld) -> None:
        owner, maxed, free_a, free_b = await _issue_owner(slot_world)
        await slot_world.set_plan(maxed, "free", "external_billing")
        change = SlotChange(maxed, "promax", "external_billing")
        with slot_mode("shadow"), structlog.testing.capture_logs() as logs:
            outcome = await _reconcile(
                slot_world, owner, cause="billing_snapshot", allow_suspend=True, change=change
            )
        assert outcome.mode == "shadow"
        assert set(outcome.suspended) == {maxed, free_b}  # would be
        assert await slot_world.rows(owner) == {}
        assert await slot_world.state(owner) is None
        would = [e for e in logs if e["event"] == "workspace_slots_would_change"]
        assert len(would) == 1
        assert set(would[0]["would_suspend"]) == {str(maxed), str(free_b)}

        # A row left from an enforce period that should resume is deleted.
        await slot_world.suspend_row(free_b, owner)
        await slot_world.set_plan(free_a, "pro", "admin_grant")
        with slot_mode("shadow"):
            outcome = await _reconcile(slot_world, owner, cause="sweep", allow_suspend=False)
        assert outcome.resumed == (free_b,)
        assert await slot_world.rows(owner) == {}

    async def test_shadow_never_fails_its_caller(self, slot_world: SlotWorld, monkeypatch) -> None:
        owner, *_ = await _issue_owner(slot_world)

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(slots, "_read_owner", broken)
        with slot_mode("shadow"), structlog.testing.capture_logs() as logs:
            async with slot_world.maker() as s:
                outcome = await reconcile_workspace_slots(
                    s, owner, cause="billing_snapshot", allow_suspend=True
                )
                assert await s.scalar(text("SELECT 1")) == 1
        assert outcome.suspended == ()
        assert [e["event"] for e in logs] == ["workspace_slots_shadow_failed"]

    async def test_an_ownership_move_replaces_the_stale_row(self, slot_world: SlotWorld) -> None:
        previous = await slot_world.user("prev")
        owner = await slot_world.user("next")
        keep = await slot_world.workspace(
            owner, plan="pro", source="external_billing", current=True
        )
        moved = await slot_world.workspace(previous)
        other = await slot_world.workspace(owner)
        # ``moved`` was suspended for the previous owner and then handed over;
        # the previous owner's cleanup never ran.
        await slot_world.suspend_row(moved, previous)
        await slot_world.set_owner(moved, owner)
        # The new owner's subscription ends: cap 1, and the default suspends
        # ``moved`` too. No primary-key error; the row is now the new owner's.
        await slot_world.set_plan(keep, "free", "external_billing")
        with slot_mode("enforce"):
            outcome = await _reconcile(
                slot_world,
                owner,
                cause="billing_snapshot",
                allow_suspend=True,
                change=SlotChange(keep, "pro", "external_billing"),
            )
        assert set(outcome.suspended) == {moved, other}
        assert await slot_world.rows(owner) == {moved: owner, other: owner}
        assert await slot_world.rows(previous) == {}
        assert (await slot_world.row_of(moved)).owner_user_id == owner

    async def test_a_foreign_row_on_an_owned_workspace_is_removed(
        self, slot_world: SlotWorld
    ) -> None:
        previous = await slot_world.user("prev")
        owner = await slot_world.user("next")
        moved = await slot_world.workspace(previous)
        await slot_world.suspend_row(moved, previous)
        await slot_world.set_owner(moved, owner)
        with slot_mode("enforce"):
            outcome = await _reconcile(
                slot_world, owner, cause="ownership_transfer", allow_suspend=False
            )
        assert outcome.pruned == 1
        assert await slot_world.row_of(moved) is None

    async def test_rows_of_workspaces_no_longer_owned_are_pruned(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user()
        live = await slot_world.workspace(owner, current=True)
        gone = await slot_world.workspace(owner)
        await slot_world.suspend_row(gone, owner)
        await slot_world.soft_delete(gone)
        with slot_mode("enforce"):
            outcome = await _reconcile(slot_world, owner, cause="sweep", allow_suspend=False)
        assert (outcome.pruned, outcome.resumed) == (1, ())
        assert await slot_world.rows(owner) == {}
        assert live not in await slot_world.rows()

    async def test_a_row_on_a_contracted_workspace_is_cleared_without_a_version_bump(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user()
        paid = await slot_world.workspace(owner, plan="basic", source="external_billing")
        await slot_world.suspend_row(paid, owner)
        with slot_mode("enforce"):
            outcome = await _reconcile(slot_world, owner, cause="sweep", allow_suspend=False)
        assert outcome.resumed == ()
        assert await slot_world.rows(owner) == {}
        assert await slot_world.state(owner) is None

    async def test_a_rolled_back_reconcile_leaves_no_rows(self, slot_world: SlotWorld) -> None:
        owner, maxed, *_ = await _issue_owner(slot_world)
        await slot_world.set_plan(maxed, "free", "external_billing")
        with slot_mode("enforce"):
            async with slot_world.maker() as s:
                assert await owner_slot_lock(s, owner, wait=True)
                outcome = await reconcile_workspace_slots(
                    s,
                    owner,
                    cause="billing_snapshot",
                    allow_suspend=True,
                    change=SlotChange(maxed, "promax", "external_billing"),
                )
                assert len(outcome.suspended) == 2
                await s.rollback()
        assert await slot_world.rows(owner) == {}
        assert await slot_world.state(owner) is None

    async def test_a_saved_selection_is_applied_on_a_drop_without_requiring_one(
        self, slot_world: SlotWorld
    ) -> None:
        owner, maxed, free_a, free_b = await _issue_owner(slot_world)
        async with slot_world.maker() as s:
            await slots._write_state(
                s,
                owner,
                now=T0,
                version=3,
                selection_required_since=None,
                keep_ids=[free_b],
                selection_saved_at=T0,
            )
            await s.commit()
        await slot_world.set_plan(maxed, "free", "external_billing")
        with slot_mode("enforce"):
            outcome = await _reconcile(
                slot_world,
                owner,
                cause="billing_snapshot",
                allow_suspend=True,
                change=SlotChange(maxed, "promax", "external_billing"),
            )
        assert set(outcome.suspended) == {maxed, free_a}
        state = await slot_world.state(owner)
        assert (state.selection_required_since, state.version) == (None, 4)


class TestSeedDefaultSuspension:
    async def test_seed_writes_the_default_drop_and_is_idempotent(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("seed")
        first = await slot_world.workspace(owner, current=True)
        second = await slot_world.workspace(owner)
        third = await slot_world.workspace(owner)
        for mode in ("off", "enforce"):
            with slot_mode(mode):
                async with slot_world.maker() as s:
                    plan = await seed_default_suspension(s, owner, now=T0)
                    await s.commit()
            assert plan.active == (first,)
            assert set(plan.suspended) == {second, third}
        assert await slot_world.rows(owner) == {second: owner, third: owner}
        state = await slot_world.state(owner)
        assert (state.selection_required_since, state.version) == (T0, 1)
        with slot_mode("enforce"):
            async with slot_world.maker() as s:
                assert await owner_selection_required(s, owner) is True


class TestRowLocksInOneOrderedStatement:
    async def test_foreign_and_own_rows_are_locked_together_in_workspace_order(
        self, slot_world: SlotWorld, async_engine
    ) -> None:
        """Review C1: workspaces moved both ways between two owners leave rows
        that both owners' reconciles delete; each locks all of them in ONE
        ordered statement, so the two never lock them in opposite orders."""
        a = await slot_world.user("cross-a")
        b = await slot_world.user("cross-b")
        x = await slot_world.workspace(a, current=True)  # was B's, row stamped for B
        y = await slot_world.workspace(b, current=True)  # was A's, row stamped for A
        await slot_world.suspend_row(x, b)
        await slot_world.suspend_row(y, a)
        with slot_mode("enforce"), StatementRecorder(async_engine) as recorder:
            outcome = await _reconcile(slot_world, a, cause="sweep", allow_suspend=False)
        locking = [
            s
            for s in recorder.statements
            if "FROM workspace_slot_suspensions" in s and "FOR UPDATE" in s
        ]
        assert len(locking) == 1, recorder.statements
        assert "ORDER BY workspace_slot_suspensions.workspace_id" in locking[0]
        assert outcome.pruned == 2
        assert await slot_world.row_of(x) is None
        assert await slot_world.row_of(y) is None
