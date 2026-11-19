"""The account operations of workspace slots on real PostgreSQL (#2918).

``get_slot_overview``, ``preview_selection`` and ``save_selection``: the
issue's example (term end, selection, upgrade), the errors of design 12.1
(422, ``SLOT-002``, ``SLOT-003``) and two selections at once.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from services.workspace_slots import (
    SlotChange,
    get_slot_overview,
    owner_slot_lock,
    preview_selection,
    reconcile_workspace_slots,
    save_selection,
)
from utils.exceptions import (
    ValidationError,
    WorkspaceSlotsFullError,
    WorkspaceSlotVersionConflictError,
)

from ._slot_world import SlotWorld, slot_mode

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _drop(world: SlotWorld, owner: str, wid: UUID, plan: str, source: str) -> None:
    """``wid``'s contract ended (it was ``plan`` / ``source``): reconcile as a drop."""
    async with world.maker() as s:
        assert await owner_slot_lock(s, owner, wait=True)
        await reconcile_workspace_slots(
            s,
            owner,
            cause="billing_snapshot",
            allow_suspend=True,
            change=SlotChange(wid, plan, source),
        )
        await s.commit()


async def _overview(world: SlotWorld, owner: str):
    async with world.maker() as s:
        return await get_slot_overview(s, owner)


async def _preview(world: SlotWorld, owner: str, ids, scenario="current"):
    async with world.maker() as s:
        return await preview_selection(s, owner, ids, scenario)


async def _save(world: SlotWorld, owner: str, ids, version: int):
    async with world.maker() as s:
        # The route's authentication read happens first on the same session.
        await s.execute(text("SELECT 1"))
        return await save_selection(s, owner, ids, version)


async def _issue_owner(world: SlotWorld):
    owner = await world.user("issue")
    personal = await world.workspace(owner, current=True)
    research = await world.workspace(owner, plan="promax", source="external_billing")
    side = await world.workspace(owner)
    return owner, personal, research, side


class TestIssueExampleThroughTheAccountOperations:
    async def test_term_end_selection_swap_and_upgrade(self, slot_world: SlotWorld) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        with slot_mode("enforce"):
            # Before the term ends: everything active; the what-if view says
            # which would stay active.
            before = await _overview(slot_world, owner)
            assert (before.owned_count, before.active_count, before.contracted_count) == (3, 3, 1)
            assert (before.allowance.cap, before.allowance.tier) == (20, "promax")
            assert (before.allowance.next_tier, before.allowance.cap_on_next_tier) == (None, None)
            assert before.active_slots == 20
            assert before.if_paid_plans_end is not None
            assert before.if_paid_plans_end.cap == 1
            assert before.if_paid_plans_end.active == (personal,)
            assert set(before.if_paid_plans_end.suspended) == {research, side}
            assert before.selection_required is False
            assert before.version == 0

            # The term ends: Max -> Free. Default: the current one stays.
            await slot_world.set_plan(research, "free", "external_billing")
            await _drop(slot_world, owner, research, "promax", "external_billing")
            after = await _overview(slot_world, owner)
            assert [w.id for w in after.workspaces] == [personal, research, side]  # by age
            assert {w.id: w.status for w in after.workspaces} == {
                personal: "active",
                research: "slot_suspended",
                side: "slot_suspended",
            }
            assert all((w.suspended_at is None) == (w.status == "active") for w in after.workspaces)
            assert (after.active_count, after.active_slots, after.allowance.cap) == (1, 1, 1)
            assert (after.allowance.next_tier, after.allowance.cap_on_next_tier) == ("pro", 3)
            assert after.selection_required is True
            assert after.if_paid_plans_end is None
            assert [w.is_current for w in after.workspaces] == [True, False, False]

            # The owner keeps Research instead: preview, then save.
            preview = await _preview(slot_world, owner, [research])
            assert preview.active == (research,)
            assert set(preview.suspended) == {personal, side}  # the complete set
            assert preview.changes.resume == (research,)
            assert preview.changes.suspend == (personal,)
            assert (preview.cap, preview.active_slots, preview.unmet) == (1, 1, ())
            saved = await _save(slot_world, owner, [research], after.version)
            assert {w.id: w.status for w in saved.workspaces} == {
                personal: "slot_suspended",
                research: "active",
                side: "slot_suspended",
            }
            assert saved.selection_required is False
            assert saved.selection.workspace_ids == (research,)
            assert saved.selection.saved_at is not None
            assert saved.version > after.version
            assert await slot_world.row_of(personal) is not None
            assert (await slot_world.row_of(personal)).cause == "selection"

            # An upgrade of a suspended workspace restores all three, each
            # with its own plan.
            await slot_world.set_plan(side, "promax", "external_billing")
            async with slot_world.maker() as s:
                assert await owner_slot_lock(s, owner, wait=True)
                await reconcile_workspace_slots(
                    s,
                    owner,
                    cause="billing_snapshot",
                    allow_suspend=True,
                    change=SlotChange(side, "free", "admin_grant"),
                )
                await s.commit()
            upgraded = await _overview(slot_world, owner)
            assert {w.id: (w.status, w.plan_name) for w in upgraded.workspaces} == {
                personal: ("active", "free"),
                research: ("active", "free"),
                side: ("active", "promax"),
            }
            assert upgraded.selection_required is False

    async def test_paid_plans_end_preview_with_a_selection(self, slot_world: SlotWorld) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        with slot_mode("enforce"):
            preview = await _preview(slot_world, owner, [side], "paid_plans_end")
        assert (preview.scenario, preview.cap, preview.active_slots) == ("paid_plans_end", 1, 1)
        assert preview.active == (side,)
        assert set(preview.suspended) == {personal, research}
        assert set(preview.changes.suspend) == {personal, research}
        assert preview.changes.resume == ()
        assert preview.unmet == ()

    async def test_a_selection_before_the_term_end_is_stored_and_applied_at_the_drop(
        self, slot_world: SlotWorld
    ) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        with slot_mode("enforce"):
            stored = await _save(slot_world, owner, [side], 0)
            assert stored.active_count == 3  # nothing suspended: stored only
            assert stored.selection.workspace_ids == (side,)
            assert stored.if_paid_plans_end.active == (side,)
            await slot_world.set_plan(research, "free", "external_billing")
            await _drop(slot_world, owner, research, "promax", "external_billing")
            after = await _overview(slot_world, owner)
        assert {w.id for w in after.workspaces if w.status == "active"} == {side}
        assert after.selection_required is False  # the owner had chosen

    async def test_an_owner_of_nothing_gets_an_empty_overview(self, slot_world: SlotWorld) -> None:
        member = await slot_world.user("member")
        other = await slot_world.user("other")
        await slot_world.workspace(other, members={member: "admin"})
        with slot_mode("enforce"):
            overview = await _overview(slot_world, member)
        assert overview.workspaces == ()
        assert (overview.owned_count, overview.active_count) == (0, 0)
        assert overview.active_slots == overview.allowance.cap == 1
        assert overview.if_paid_plans_end is None
        assert overview.selection_required is False

    async def test_the_bonus_counts_in_the_allowance(self, slot_world: SlotWorld) -> None:
        owner = await slot_world.user("bonus", bonus=2)
        await slot_world.workspace(owner)
        with slot_mode("enforce"):
            overview = await _overview(slot_world, owner)
        assert (overview.allowance.cap, overview.allowance.slot_bonus) == (3, 2)
        assert (overview.allowance.next_tier, overview.allowance.cap_on_next_tier) == ("pro", 5)


class TestSelectionErrors:
    async def test_ids_not_owned_duplicated_or_unknown_are_one_422(
        self, slot_world: SlotWorld
    ) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        stranger = await slot_world.user("stranger")
        foreign = await slot_world.workspace(stranger)
        unknown = uuid4()
        messages = set()
        with slot_mode("enforce"):
            for ids, invalid in (
                ([personal, foreign], [str(foreign)]),
                ([unknown], [str(unknown)]),
                ([personal, personal], [str(personal)]),
            ):
                for operation in ("preview", "save"):
                    with pytest.raises(ValidationError) as refused:
                        if operation == "preview":
                            await _preview(slot_world, owner, ids)
                        else:
                            await _save(slot_world, owner, ids, 0)
                    assert refused.value.error_code == "VAL-001"
                    assert refused.value.status_code == 422
                    assert refused.value.details["field"] == "workspace_ids"
                    assert refused.value.details["invalid_ids"] == invalid
                    messages.add(refused.value.message)
        assert len(messages) == 1
        assert (await _overview_off(slot_world, owner)).version == 0

    async def test_a_stale_version_is_slot_003_and_saves_nothing(
        self, slot_world: SlotWorld
    ) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        with slot_mode("enforce"):
            await _save(slot_world, owner, [side], 0)
            with pytest.raises(WorkspaceSlotVersionConflictError) as stale:
                await _save(slot_world, owner, [research], 0)
            current = await _overview(slot_world, owner)
        assert stale.value.error_code == "SLOT-003"
        assert stale.value.details == {"expected_version": 0, "current_version": current.version}
        assert current.selection.workspace_ids == (side,)

    async def test_contracted_workspaces_filling_every_slot_is_slot_002(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("contracted")
        paid = await slot_world.workspace(owner, plan="basic", source="external_billing")
        free = await slot_world.workspace(owner)
        await slot_world.suspend_row(free, owner)
        with slot_mode("enforce"):
            preview = await _preview(slot_world, owner, [free])
            assert preview.unmet == (free,)
            assert preview.active == (paid,)
            with pytest.raises(WorkspaceSlotsFullError) as full:
                await _save(slot_world, owner, [free], 0)
            # Naming only active workspaces is not refused.
            await _save(slot_world, owner, [paid], 0)
        assert full.value.error_code == "SLOT-002"
        assert full.value.details == {
            "cap": 1,
            "active_slots": 1,
            "active_count": 1,
            "contracted_count": 1,
            "next_tier": "pro",
            "cap_on_next_tier": 3,
            "unmet": [str(free)],
        }

    async def test_two_selections_at_once_give_one_save_and_one_slot_003(
        self, slot_world: SlotWorld
    ) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        await slot_world.set_plan(research, "free", "external_billing")
        with slot_mode("enforce"):
            await _drop(slot_world, owner, research, "promax", "external_billing")
            version = (await _overview(slot_world, owner)).version
            results = await asyncio.gather(
                _save(slot_world, owner, [research], version),
                _save(slot_world, owner, [side], version),
                return_exceptions=True,
            )
            final = await _overview(slot_world, owner)
        saved = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, BaseException)]
        assert len(saved) == 1 and len(refused) == 1
        assert isinstance(refused[0], WorkspaceSlotVersionConflictError)
        winner = saved[0].selection.workspace_ids
        assert final.selection.workspace_ids == winner
        assert [w.id for w in final.workspaces if w.status == "active"] == list(winner)


async def _overview_off(world: SlotWorld, owner: str):
    async with world.maker() as s:
        return await get_slot_overview(s, owner)
