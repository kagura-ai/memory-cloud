"""Workspace slots across ownership changes, erasure, the sweep and the CLI (#2918).

Delete and transfers reconcile both owners after their commit (resume only;
a failure is left to the sweep and never fails the committed change). A
voluntary transfer in ``enforce`` needs a free slot at the recipient
(``SLOT-004``), so a transfer round trip gains nothing. Erasure and user
deletion remove the subject's rows in every mode, also before the migration.
The sweep resumes and prunes, never suspends.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog
from sqlalchemy import select, text, update

from cli.workspace_slots import _main as cli_main
from cli.workspace_slots import _parse as cli_parse
from models.auth import User
from services import workspace_slots as slots
from services.user_deletion_service import delete_user_rows
from services.workspace_ownership_service import WorkspaceOwnershipService
from services.workspace_service import WorkspaceService
from services.workspace_slots import delete_owner_slot_rows, sweep_workspace_slots
from utils.exceptions import (
    NotFoundException,
    WorkspaceSlotsBusyError,
    WorkspaceSlotTransferRefusedError,
)

from ._slot_world import T0, SlotWorld, StatementRecorder, slot_mode
from .test_erasure_lock_order_db import _external_stores_stubbed

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _owner_over_cap(world: SlotWorld, prefix: str = "own"):
    """X (current, active), Y and Z suspended for the owner: cap 1."""
    owner = await world.user(prefix)
    x = await world.workspace(owner, current=True)
    y = await world.workspace(owner)
    z = await world.workspace(owner)
    await world.suspend_row(y, owner)
    await world.suspend_row(z, owner)
    return owner, x, y, z


async def _transfer(world: SlotWorld, wid, current_owner: str, target: str):
    async with world.maker() as s:
        return await WorkspaceOwnershipService(s).transfer_ownership(
            workspace_id=wid,
            current_owner_id=current_owner,
            target_user_id=target,
            performed_by_email="owner@example.com",
        )


async def _delete(world: SlotWorld, wid, owner: str) -> None:
    async with world.maker() as s:
        await WorkspaceService(s).delete_workspace(wid, deleted_by=owner)


class TestDelete:
    async def test_a_delete_resumes_a_suspended_workspace_after_its_commit(
        self, slot_world: SlotWorld
    ) -> None:
        owner, x, y, z = await _owner_over_cap(slot_world)
        with slot_mode("enforce"):
            await _delete(slot_world, x, owner)
        # The default order resumes the older one.
        assert await slot_world.rows(owner) == {z: owner}

    async def test_off_runs_no_slot_statement(self, slot_world: SlotWorld, async_engine) -> None:
        owner, x, y, z = await _owner_over_cap(slot_world)
        with slot_mode("off"), StatementRecorder(async_engine) as recorder:
            await _delete(slot_world, x, owner)
        assert recorder.touching_slot_tables() == []
        assert await slot_world.rows(owner) == {y: owner, z: owner}

    async def test_a_failed_post_commit_reconcile_never_fails_the_delete(
        self, slot_world: SlotWorld, monkeypatch
    ) -> None:
        owner, x, y, z = await _owner_over_cap(slot_world)

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        with slot_mode("enforce"):
            with monkeypatch.context() as patched, structlog.testing.capture_logs() as logs:
                patched.setattr(slots, "_reconcile", broken)
                await _delete(slot_world, x, owner)
            assert [
                (e["reason"], e["cause"]) for e in logs if e["event"] == "workspace_slots_deferred"
            ] == [("error", "workspace_delete")]
            assert await slot_world.rows(owner) == {y: owner, z: owner}
            # The sweep finishes the resume.
            async with slot_world.maker() as s:
                outcome = await sweep_workspace_slots(s)
        assert outcome.resumed >= 1
        assert await slot_world.rows(owner) == {z: owner}

    async def test_a_busy_owner_lock_defers_the_resume_to_the_sweep(
        self, slot_world: SlotWorld, monkeypatch
    ) -> None:
        monkeypatch.setattr(slots, "_WAIT_LOCK_TIMEOUT", "200ms")
        owner, x, y, z = await _owner_over_cap(slot_world)
        release = await slot_world.hold_owner_lock(owner)
        try:
            with slot_mode("enforce"), structlog.testing.capture_logs() as logs:
                await _delete(slot_world, x, owner)
        finally:
            await release()
        assert [e["reason"] for e in logs if e["event"] == "workspace_slots_deferred"] == [
            "lock_busy"
        ]
        assert await slot_world.rows(owner) == {y: owner, z: owner}


class TestTransfer:
    async def test_a_recipient_without_a_free_slot_is_slot_004_in_enforce_only(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("giver")
        recipient = await slot_world.user("taker")
        await slot_world.workspace(recipient, current=True)  # uses the recipient's slot
        gift = await slot_world.workspace(owner, members={recipient: "admin"})
        with slot_mode("enforce"), pytest.raises(WorkspaceSlotTransferRefusedError) as refused:
            await _transfer(slot_world, gift, owner, recipient)
        assert (refused.value.status_code, refused.value.error_code) == (409, "SLOT-004")
        assert refused.value.details == {}
        assert await slot_world.owner_of(gift) == owner
        with slot_mode("off"):
            await _transfer(slot_world, gift, owner, recipient)
        assert await slot_world.owner_of(gift) == recipient

    async def test_a_contracted_workspace_into_a_full_account_is_refused(
        self, slot_world: SlotWorld
    ) -> None:
        """Review A2: the recipient needs a truly free slot; a contracted
        workspace would only be active by pushing an active one out."""
        owner = await slot_world.user("giver")
        recipient = await slot_world.user("taker")
        await slot_world.workspace(recipient, current=True)
        paid = await slot_world.workspace(
            owner, plan="basic", source="external_billing", members={recipient: "admin"}
        )
        with slot_mode("enforce"), pytest.raises(WorkspaceSlotTransferRefusedError):
            await _transfer(slot_world, paid, owner, recipient)
        assert await slot_world.owner_of(paid) == owner

    async def test_an_incoming_higher_tier_makes_room(self, slot_world: SlotWorld) -> None:
        owner = await slot_world.user("giver")
        recipient = await slot_world.user("taker")
        await slot_world.workspace(recipient, current=True)
        pro = await slot_world.workspace(
            owner, plan="pro", source="admin_grant", members={recipient: "admin"}
        )
        with slot_mode("enforce"):
            await _transfer(slot_world, pro, owner, recipient)
        assert await slot_world.owner_of(pro) == recipient

    @pytest.mark.parametrize("variant", ["saved_selection", "billed_basic"])
    async def test_a_round_trip_through_an_empty_account_gains_nothing(
        self, slot_world: SlotWorld, variant: str
    ) -> None:
        """Review A2 (a) and (b): W1 leaves, W2 resumes, W1 cannot come back."""
        owner = await slot_world.user("trip")
        second = await slot_world.user("trip-second")
        if variant == "billed_basic":
            w1 = await slot_world.workspace(
                owner,
                plan="basic",
                source="external_billing",
                current=True,
                members={second: "admin"},
            )
        else:
            w1 = await slot_world.workspace(owner, current=True, members={second: "admin"})
        w2 = await slot_world.workspace(owner)
        await slot_world.suspend_row(w2, owner)
        if variant == "saved_selection":
            async with slot_world.maker() as s:
                await slots._write_state(
                    s,
                    owner,
                    now=T0,
                    version=1,
                    selection_required_since=None,
                    keep_ids=[w1],
                    selection_saved_at=T0,
                )
                await s.commit()
        with slot_mode("enforce"):
            await _transfer(slot_world, w1, owner, second)
            assert await slot_world.rows(owner) == {}  # W2 resumed
            with pytest.raises(WorkspaceSlotTransferRefusedError):
                await _transfer(slot_world, w1, second, owner)
        assert await slot_world.owner_of(w1) == second

    async def test_a_busy_recipient_lock_is_a_retryable_409(self, slot_world: SlotWorld) -> None:
        owner = await slot_world.user("giver")
        recipient = await slot_world.user("taker")
        gift = await slot_world.workspace(owner, members={recipient: "admin"})
        release = await slot_world.hold_owner_lock(recipient)
        try:
            with slot_mode("enforce"), pytest.raises(WorkspaceSlotsBusyError):
                await _transfer(slot_world, gift, owner, recipient)
        finally:
            await release()
        assert await slot_world.owner_of(gift) == owner

    async def test_a_transfer_round_trip_gains_nothing(self, slot_world: SlotWorld) -> None:
        owner, x, y, z = await _owner_over_cap(slot_world, "roundtrip")
        second = await slot_world.user("second-account")
        async with slot_world.maker() as s:
            from models.auth import WorkspaceMember

            s.add(WorkspaceMember(workspace_id=x, user_id=second, role="admin"))
            await s.commit()
        with slot_mode("enforce"):
            # X goes to the second account (a free slot there); both owners
            # are reconciled after the commit: Y resumes for the first.
            await _transfer(slot_world, x, owner, second)
            assert await slot_world.rows(owner) == {z: owner}
            # Back again: the first account's only slot is taken by Y now.
            with pytest.raises(WorkspaceSlotTransferRefusedError):
                await _transfer(slot_world, x, second, owner)
        assert await slot_world.owner_of(x) == second
        assert await slot_world.rows(owner) == {z: owner}

    async def test_a_force_transfer_reconciles_both_owners_after_its_commit(
        self, slot_world: SlotWorld
    ) -> None:
        owner, x, y, z = await _owner_over_cap(slot_world, "forced")
        target = await slot_world.user("seizer")
        with slot_mode("enforce"):
            async with slot_world.maker() as s:
                result = await WorkspaceOwnershipService(s).force_transfer_ownership(
                    workspace_id=x,
                    target_user_id=target,
                    performed_by_user_id="slot-admin",
                    performed_by_email="admin@example.com",
                    reason="owner unavailable",
                )
        assert result.changed
        # No recipient check for a force transfer; the previous owner's freed
        # slot resumes Y.
        assert await slot_world.owner_of(x) == target
        assert await slot_world.rows(owner) == {z: owner}

    async def test_an_approved_dual_control_transfer_reconciles_both_owners(
        self, slot_world: SlotWorld
    ) -> None:
        owner, x, y, z = await _owner_over_cap(slot_world, "approved")
        target = await slot_world.user("approved-target")
        with slot_mode("enforce"):
            async with slot_world.maker() as s:
                request = await WorkspaceOwnershipService(s).initiate_force_transfer(
                    workspace_id=x,
                    target_user_id=target,
                    performed_by_user_id="slot-admin-1",
                    performed_by_email="admin1@example.com",
                    reason="owner unavailable",
                )
            async with slot_world.maker() as s:
                result = await WorkspaceOwnershipService(s).approve_force_transfer(
                    request_id=request.id,
                    approver_user_id="slot-admin-2",
                    approver_email="admin2@example.com",
                )
        assert result.changed
        assert await slot_world.owner_of(x) == target
        assert await slot_world.rows(owner) == {z: owner}


class TestErasureAndUserDeletion:
    async def test_erasure_removes_the_subjects_rows_in_every_mode(
        self, slot_world: SlotWorld, connector_world
    ) -> None:
        subject = await connector_world.user("erased")
        await connector_world.db.commit()
        other = await slot_world.user("bystander")
        kept = await slot_world.workspace(other)
        await slot_world.suspend_row(kept, other)
        async with slot_world.maker() as s:
            await slots._write_state(s, subject, now=T0, version=2, selection_required_since=T0)
            await s.execute(
                slots.pg_insert(slots.WorkspaceSlotSuspension).values(
                    workspace_id=slots.UUID(int=4242),
                    owner_user_id=subject,
                    cause="billing_snapshot",
                    suspended_at=T0,
                )
            )
            await s.commit()
        with slot_mode("off"), _external_stores_stubbed():
            counts = await connector_world.erase(subject)
        assert counts["workspace_slot_states"] == 1
        assert counts["workspace_slot_suspensions"] == 1
        assert await slot_world.state(subject) is None
        assert await slot_world.rows(subject) == {}
        assert await slot_world.rows(other) == {kept: other}

    async def test_user_deletion_removes_the_accounts_rows(self, slot_world: SlotWorld) -> None:
        user = await slot_world.user("deleted")
        async with slot_world.maker() as s:
            await slots._write_state(s, user, now=T0, version=1, selection_required_since=None)
            await s.commit()
        with slot_mode("off"):
            async with slot_world.maker() as s:
                await s.execute(update(User).where(User.user_id == user).values(name="locked"))
                await delete_user_rows(s, user)
                await s.commit()
        assert await slot_world.state(user) is None

    async def test_the_deletes_run_when_the_tables_are_missing(self, slot_world: SlotWorld) -> None:
        async with slot_world.maker() as s:
            await s.execute(text("DROP TABLE workspace_slot_suspensions, workspace_slot_states"))
            assert await slots.slot_tables_present(s) is False
            assert await delete_owner_slot_rows(s, "anyone") == {}
            await s.rollback()
        async with slot_world.maker() as s:
            assert await slots.slot_tables_present(s) is True


class TestSweep:
    async def test_the_sweep_resumes_prunes_and_never_suspends(self, slot_world: SlotWorld) -> None:
        # An owner over the cap from before keeps what is active: the sweep
        # never suspends, even where a drop would.
        over = await slot_world.user("sweep-over")
        for _ in range(3):
            await slot_world.workspace(over)
        stays = await slot_world.workspace(over)
        await slot_world.suspend_row(stays, over)
        # A lost resume: one of two slots free.
        resumes = await slot_world.user("sweep-resume", bonus=1)
        await slot_world.workspace(resumes, current=True)
        waiting = await slot_world.workspace(resumes)
        await slot_world.suspend_row(waiting, resumes)
        # An orphan row: its workspace is gone.
        gone_owner = await slot_world.user("sweep-gone")
        gone = await slot_world.workspace(gone_owner)
        await slot_world.workspace(gone_owner)
        await slot_world.suspend_row(gone, gone_owner)
        await slot_world.soft_delete(gone)
        with slot_mode("enforce"):
            async with slot_world.maker() as s:
                outcome = await sweep_workspace_slots(s)
        assert outcome.failed == 0
        assert await slot_world.rows(over) == {stays: over}
        assert await slot_world.rows(resumes) == {}
        assert await slot_world.rows(gone_owner) == {}

    async def test_a_busy_owner_is_skipped_and_the_batch_continues(
        self, slot_world: SlotWorld
    ) -> None:
        busy_owner = await slot_world.user("sweep-a")
        await slot_world.workspace(busy_owner, current=True)
        w1 = await slot_world.workspace(busy_owner)
        await slot_world.suspend_row(w1, busy_owner)
        # Every suspended row of this owner is gone (deleted): pruned.
        free_owner = await slot_world.user("sweep-b")
        w2 = await slot_world.workspace(free_owner)
        await slot_world.workspace(free_owner)
        await slot_world.suspend_row(w2, free_owner)
        await slot_world.soft_delete(w2)
        release = await slot_world.hold_owner_lock(busy_owner)
        try:
            with slot_mode("enforce"):
                async with slot_world.maker() as s:
                    outcome = await sweep_workspace_slots(s)
        finally:
            await release()
        assert outcome.busy >= 1
        assert await slot_world.rows(busy_owner) == {w1: busy_owner}
        assert await slot_world.rows(free_owner) == {}

    async def test_the_batch_is_bounded_and_continues_after_the_cursor(
        self, slot_world: SlotWorld
    ) -> None:
        owners = sorted([await slot_world.user("sweep-batch") for _ in range(2)])
        for owner in owners:
            w = await slot_world.workspace(owner)
            await slot_world.suspend_row(w, owner)
        with slot_mode("enforce"):
            async with slot_world.maker() as s:
                first = await sweep_workspace_slots(s, after=owners[0][:-1], limit=1)
            async with slot_world.maker() as s:
                second = await sweep_workspace_slots(s, after=first.next_after, limit=1)
        assert (first.owners, first.next_after) == (1, owners[0])
        assert second.owners == 1
        assert await slot_world.rows(owners[0]) == {}
        assert await slot_world.rows(owners[1]) == {}

    async def test_off_runs_no_statement(self, slot_world: SlotWorld, async_engine) -> None:
        with slot_mode("off"), StatementRecorder(async_engine) as recorder:
            async with slot_world.maker() as s:
                outcome = await sweep_workspace_slots(s)
        assert outcome.owners == 0
        assert recorder.statements == []


class TestCli:
    async def test_reconcile_refuses_while_off_unless_told_and_then_resumes(
        self, slot_world: SlotWorld, capsys
    ) -> None:
        owner = await slot_world.user("cli", bonus=1)
        await slot_world.workspace(owner, current=True)
        waiting = await slot_world.workspace(owner)
        await slot_world.suspend_row(waiting, owner)
        with slot_mode("off"):
            assert await cli_main(cli_parse(["reconcile", "--owner", owner])) == 2
            assert await slot_world.rows(owner) == {waiting: owner}
            assert (
                await cli_main(cli_parse(["reconcile", "--owner", owner, "--even-when-off"])) == 0
            )
        assert await slot_world.rows(owner) == {}
        with slot_mode("enforce"):
            assert await cli_main(cli_parse(["status", "--owner", owner])) == 0
        out = capsys.readouterr().out
        assert "WORKSPACE_SLOT_SUSPENSION=enforce" in out
        assert f"owner {owner}: cap 2" in out


class TestNoRowsOutliveAnErasedAccount:
    """Review B: a reconcile racing the erasure of its owner never re-creates
    the owner's state row; a selection save waits for the erasure and finds
    the account gone."""

    async def _erasing(self, slot_world: SlotWorld, user: str):
        """A second session holding what erasure holds: the users row, then
        the subject's slot rows deleted (not committed yet)."""
        session = slot_world.maker()
        await session.execute(select(User.id).where(User.user_id == user).with_for_update())
        await delete_owner_slot_rows(session, user)
        return session

    async def test_a_sweep_racing_the_erasure_leaves_no_state_row(
        self, slot_world: SlotWorld
    ) -> None:
        user = await slot_world.user("racing", bonus=1)
        await slot_world.workspace(user, current=True)
        waiting = await slot_world.workspace(user)
        await slot_world.suspend_row(waiting, user)
        async with slot_world.maker() as s:
            await slots._write_state(s, user, now=T0, version=3, selection_required_since=T0)
            await s.commit()
        erasure = await self._erasing(slot_world, user)
        try:
            with slot_mode("enforce"):
                sweep = asyncio.create_task(_sweep_one(slot_world, user))
                await asyncio.sleep(0.5)
                assert not sweep.done()  # waiting on the erasure's row locks
                await erasure.commit()
                await asyncio.wait_for(sweep, 10)
        finally:
            await erasure.close()
        assert await slot_world.state(user) is None
        assert await slot_world.rows(user) == {}

    async def test_a_selection_save_during_the_erasure_finds_the_account_gone(
        self, slot_world: SlotWorld
    ) -> None:
        user = await slot_world.user("racing-put")
        await slot_world.workspace(user, current=True)
        async with slot_world.maker() as s:
            await slots._write_state(s, user, now=T0, version=0, selection_required_since=None)
            await s.commit()
        erasure = await self._erasing(slot_world, user)
        try:
            with slot_mode("enforce"):
                put = asyncio.create_task(_save_selection(slot_world, user))
                await asyncio.sleep(0.5)
                assert not put.done()
                await erasure.execute(
                    text("UPDATE users SET current_workspace_id = NULL WHERE user_id = :u"),
                    {"u": user},
                )
                await erasure.execute(
                    text("DELETE FROM workspace_members WHERE user_id = :u"), {"u": user}
                )
                await erasure.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": user})
                await erasure.commit()
                with pytest.raises(NotFoundException):
                    await asyncio.wait_for(put, 10)
        finally:
            await erasure.close()
        assert await slot_world.state(user) is None


async def _sweep_one(world: SlotWorld, owner: str) -> None:
    async with world.maker() as s:
        assert await slots.owner_slot_lock(s, owner, wait=True)
        await slots.reconcile_workspace_slots(s, owner, cause="sweep", allow_suspend=False)
        await s.commit()


async def _save_selection(world: SlotWorld, owner: str):
    async with world.maker() as s:
        return await slots.save_selection(s, owner, [], 0)
