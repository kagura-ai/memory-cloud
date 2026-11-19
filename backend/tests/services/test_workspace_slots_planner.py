"""The pure workspace-slot planner and allowance rules (Issue #2918).

``plan_active_set`` decides which owned workspaces stay active;
``slot_allowance`` / ``is_allowance_drop`` / ``change_is_non_decreasing`` say
what the allowance is and whether an event is a drop: an event that ends a paid
contract, lowers a plan's owned-workspace grant or lowers the slot bonus, and
does not raise the allowance. The exhaustive test
walks every combination over up to four workspaces (under contract or not,
active or suspended now, selected or not, the owner's current one), caps 1..4,
drop and non-drop, and asserts the invariants the transition relies on.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import product
from uuid import UUID

import pytest

from services.workspace_slots import (
    OwnedWorkspace,
    SlotChange,
    change_is_non_decreasing,
    is_allowance_drop,
    plan_active_set,
    plan_transition,
    slot_allowance,
)

BASE = datetime(2026, 1, 1)
IDS = [UUID(int=i + 1) for i in range(4)]
NOT_OWNED = UUID(int=99)
# Creation order differs from id order so "older first" is exercised.
CREATED_OFFSET_DAYS = [3, 0, 2, 1]
# (plan, entitlement source) per position: uncontracted ones mix tiers and
# sources (an admin-granted Pro ranks above a billed Free), contracted ones are
# paid tiers owned by billing.
UNCONTRACTED = [
    ("free", "external_billing"),
    ("pro", "admin_grant"),
    ("free", "admin_grant"),
    ("basic", "admin_grant"),
]
CONTRACTED = [
    ("basic", "external_billing"),
    ("pro", "external_billing"),
    ("promax", "external_billing"),
    ("basic", "external_billing"),
]


def _ws(
    i: int, *, contracted: bool = False, suspended: bool = False, plan: str | None = None
) -> OwnedWorkspace:
    plan_name, source = (CONTRACTED if contracted else UNCONTRACTED)[i]
    if plan is not None:
        plan_name = plan
    return OwnedWorkspace(
        id=IDS[i],
        plan_name=plan_name,
        entitlement_source=source,
        created_at=BASE + timedelta(days=CREATED_OFFSET_DAYS[i]),
        suspended=suspended,
    )


def _replanned(owned: list[OwnedWorkspace], suspended: set[UUID]) -> list[OwnedWorkspace]:
    return [
        OwnedWorkspace(
            id=w.id,
            plan_name=w.plan_name,
            entitlement_source=w.entitlement_source,
            created_at=w.created_at,
            suspended=w.id in suspended,
        )
        for w in owned
    ]


def _cases():
    for size in range(1, 5):
        positions = range(size)
        for contracted, suspended, kept in product(
            product((False, True), repeat=size),
            product((False, True), repeat=size),
            product((False, True), repeat=size),
        ):
            owned = [_ws(i, contracted=contracted[i], suspended=suspended[i]) for i in positions]
            # Selection in reverse id order (not the default order), with an id
            # the owner does not own and a duplicate, both ignored.
            keep = [IDS[i] for i in reversed(positions) if kept[i]]
            keep_ids = [NOT_OWNED, *keep, *keep[:1]]
            for current in (None, *IDS[:size]):
                yield owned, keep, keep_ids, current


class TestPlannerInvariantsExhaustive:
    """Every combination over up to four workspaces, caps 1..4, drop and not."""

    def test_invariants_hold_for_every_combination(self) -> None:
        checked = 0
        for owned, keep, keep_ids, current in _cases():
            ids = {w.id for w in owned}
            contracted = {w.id for w in owned if w.under_billing_contract}
            active_now = {w.id for w in owned if not w.suspended}
            for cap, drop in product(range(1, 5), (False, True)):
                plan = plan_active_set(
                    owned, cap, drop=drop, keep_ids=keep_ids, current_workspace_id=current
                )
                active, suspended = set(plan.active), set(plan.suspended)
                label = (len(owned), sorted(contracted), sorted(active_now), keep, current, cap)
                allowance = max(cap, len(contracted))

                # A partition of the owned set, no duplicates.
                assert active | suspended == ids, label
                assert not active & suspended, label
                assert len(plan.active) == len(active), label
                assert len(plan.suspended) == len(suspended), label
                assert plan.allowance == allowance, label
                assert plan.contracted_count == len(contracted), label
                assert plan.drop is drop, label

                # A workspace somebody pays for is always active.
                assert contracted <= active, label

                if drop:
                    # On a drop exactly min(owned, allowance) stay active.
                    assert plan.active_slots == allowance, label
                    assert len(active) == min(len(owned), allowance), label
                else:
                    # Without a drop the active count never shrinks.
                    assert plan.active_slots == max(allowance, len(active_now)), label
                    assert len(active) >= len(active_now), label
                    assert len(active) == min(len(owned), plan.active_slots), label

                # Everything fits: everything is active.
                if len(owned) <= cap:
                    assert suspended == set(), label

                # Priority: a selected (uncontracted) workspace left suspended
                # means every active slot went to a contracted or selected one.
                selected = [wid for wid in keep if wid not in contracted]
                assert set(plan.unmet) == set(selected) & suspended, label
                assert list(plan.unmet) == [wid for wid in selected if wid in suspended], label
                if plan.unmet:
                    assert active <= contracted | set(selected), label
                # ... then the workspaces active now before the suspended ones.
                others_now_active = active_now - contracted - set(selected)
                others_suspended = ids - active_now - contracted - set(selected)
                if others_now_active & suspended:
                    assert not others_suspended & active, label

                # Idempotent: planning the result again changes nothing, with or
                # without a drop.
                again = _replanned(owned, suspended)
                for drop_again in (drop, False):
                    replan = plan_active_set(
                        again,
                        cap,
                        drop=drop_again,
                        keep_ids=keep_ids,
                        current_workspace_id=current,
                    )
                    assert set(replan.active) == active, (label, drop_again)

                # Deterministic: the input order does not matter.
                assert (
                    plan_active_set(
                        list(reversed(owned)),
                        cap,
                        drop=drop,
                        keep_ids=keep_ids,
                        current_workspace_id=current,
                    )
                    == plan
                ), label
                checked += 1
        # 1..4 workspaces: sum(8**n * (n + 1)) combinations x 4 caps x 2.
        assert checked == sum(8**n * (n + 1) for n in range(1, 5)) * 8


class TestDefaultOrder:
    def test_current_then_higher_plan_then_older_then_id(self) -> None:
        # All four active now and uncontracted; cap 1 and a drop keep one.
        same_age = BASE
        owned = [
            OwnedWorkspace(IDS[0], "free", "admin_grant", same_age + timedelta(days=1)),
            OwnedWorkspace(IDS[1], "pro", "admin_grant", same_age + timedelta(days=5)),
            OwnedWorkspace(IDS[2], "free", "admin_grant", same_age),
            OwnedWorkspace(IDS[3], "free", "admin_grant", same_age),
        ]
        plan = plan_active_set(owned, 4, drop=False, current_workspace_id=IDS[0])
        # current first, then the higher plan, then the older, then the id.
        assert plan.active == (IDS[0], IDS[1], IDS[2], IDS[3])
        plan = plan_active_set(owned, 1, drop=True, current_workspace_id=None)
        assert plan.active == (IDS[1],)
        assert plan.suspended == (IDS[2], IDS[3], IDS[0])

    def test_selection_order_wins_over_the_default_order(self) -> None:
        owned = [_ws(i) for i in range(3)]
        plan = plan_active_set(owned, 1, drop=True, keep_ids=[IDS[2], IDS[0]])
        assert plan.active == (IDS[2],)
        assert plan.suspended[0] == IDS[0]
        assert plan.unmet == (IDS[0],)

    def test_contracted_ids_in_the_selection_are_ignored_for_priority(self) -> None:
        owned = [_ws(0, contracted=True), _ws(1), _ws(2)]
        plan = plan_active_set(owned, 2, drop=True, keep_ids=[IDS[0], IDS[2]])
        assert plan.active == (IDS[0], IDS[2])
        assert plan.unmet == ()


class TestIssueExample:
    """#2918: one Max workspace and two Free; the term ends; an upgrade later."""

    def test_issue_example_max_and_two_free_term_end_then_upgrade(self) -> None:
        maxed = OwnedWorkspace(IDS[0], "promax", "external_billing", BASE)
        free_a = OwnedWorkspace(IDS[1], "free", "admin_grant", BASE + timedelta(days=1))
        free_b = OwnedWorkspace(IDS[2], "free", "admin_grant", BASE + timedelta(days=2))
        before = [maxed, free_a, free_b]
        assert slot_allowance(before, 0).allowance == 20
        assert plan_active_set(before, 20, drop=False).suspended == ()

        # Term end: billing pushes Free for the Max workspace. The allowance
        # falls from 20 to 1: a drop.
        ended = OwnedWorkspace(IDS[0], "free", "external_billing", BASE)
        after = [ended, free_a, free_b]
        allowance = slot_allowance(after, 0)
        assert (allowance.summary.cap, allowance.allowance) == (1, 1)
        change = SlotChange(
            workspace_id=IDS[0],
            previous_plan_name="promax",
            previous_entitlement_source="external_billing",
        )
        assert is_allowance_drop(after, 0, change)

        # Without a selection the default keeps the owner's current workspace.
        default = plan_active_set(after, 1, drop=True, current_workspace_id=IDS[0])
        assert default.active == (IDS[0],)
        assert set(default.suspended) == {IDS[1], IDS[2]}

        # The owner keeps a Free workspace instead: one active, two suspended.
        chosen = plan_active_set(
            after, 1, drop=True, keep_ids=[IDS[1]], current_workspace_id=IDS[0]
        )
        assert chosen.active == (IDS[1],)
        assert set(chosen.suspended) == {IDS[0], IDS[2]}

        # Later they upgrade a suspended Free workspace to Max: the allowance
        # covers all three, so all three resume; only that one has Max. Built
        # as the reader builds it: the upgraded workspace is under a contract,
        # so the read-time rule counts it active although its row is still
        # there; the transition treats it as suspended going in.
        upgraded = OwnedWorkspace(
            IDS[2], "promax", "external_billing", BASE + timedelta(days=2), suspended=False
        )
        now = [
            OwnedWorkspace(IDS[0], "free", "external_billing", BASE, suspended=True),
            free_a,
            upgraded,
        ]
        up = SlotChange(
            workspace_id=IDS[2],
            previous_plan_name="free",
            previous_entitlement_source="admin_grant",
        )
        assert not is_allowance_drop(now, 0, up)
        decision = plan_transition(
            now,
            {IDS[0], IDS[2]},
            0,
            change=up,
            allow_suspend=True,
            keep_ids=(IDS[1],),
            current_workspace_id=IDS[0],
        )
        assert set(decision.plan.active) == {IDS[0], IDS[1], IDS[2]}
        assert set(decision.resumed) == {IDS[0], IDS[2]}
        assert decision.suspend == ()
        assert [w.plan_name for w in now] == ["free", "free", "promax"]


class TestAllowanceAndDrop:
    def test_allowance_is_the_cap_or_the_contracted_count(self) -> None:
        owned = [_ws(0, contracted=True), _ws(3, contracted=True), _ws(2)]
        allowance = slot_allowance(owned, 0)
        assert allowance.summary.cap == 1  # basic grants nothing
        assert allowance.contracted_count == 2
        assert allowance.allowance == 2

    def test_suspended_workspaces_count_in_the_tier_grant(self) -> None:
        owned = [_ws(1, suspended=True), _ws(0)]
        assert slot_allowance(owned, 0).summary.cap == 3  # admin-granted pro

    def test_max_to_free_is_a_drop(self) -> None:
        owned = [OwnedWorkspace(IDS[0], "free", "external_billing", BASE), _ws(2)]
        change = SlotChange(IDS[0], "promax", "external_billing")
        assert is_allowance_drop(owned, 0, change)

    def test_one_of_two_contracted_basics_ending_is_a_drop(self) -> None:
        owned = [
            OwnedWorkspace(IDS[0], "free", "external_billing", BASE),
            _ws(3, contracted=True),
        ]
        assert slot_allowance(owned, 0).allowance == 1
        assert is_allowance_drop(owned, 0, SlotChange(IDS[0], "basic", "external_billing"))

    def test_admin_comp_from_billed_basic_to_granted_pro_is_not_a_drop(self) -> None:
        # An owner over the cap from before: a billed Basic and three Free.
        # Comping the Basic to a granted Pro ends its contract but raises the
        # allowance from 1 to 3.
        owned = [OwnedWorkspace(IDS[0], "pro", "admin_grant", BASE)] + [
            OwnedWorkspace(IDS[i], "free", "admin_grant", BASE) for i in (1, 2, 3)
        ]
        change = SlotChange(IDS[0], "basic", "external_billing")
        assert slot_allowance(owned, 0).allowance == 3
        assert not is_allowance_drop(owned, 0, change)

    def test_same_tier_comp_is_a_drop(self) -> None:
        # The contract ends and the allowance stays 1: the excess is cut.
        owned = [OwnedWorkspace(IDS[0], "basic", "admin_grant", BASE), _ws(2)]
        assert is_allowance_drop(owned, 0, SlotChange(IDS[0], "basic", "external_billing"))

    def test_a_contract_ending_with_an_unchanged_allowance_is_a_drop(self) -> None:
        # Basic (contract, allowance max(1, 1) = 1) ends as Free: still 1.
        owned = [OwnedWorkspace(IDS[0], "free", "external_billing", BASE), _ws(2)]
        assert slot_allowance(owned, 0).allowance == 1
        assert is_allowance_drop(owned, 0, SlotChange(IDS[0], "basic", "external_billing"))

    def test_a_granted_tier_going_down_is_a_drop(self) -> None:
        owned = [OwnedWorkspace(IDS[0], "free", "admin_grant", BASE), _ws(2)]
        assert is_allowance_drop(owned, 0, SlotChange(IDS[0], "pro", "admin_grant"))

    def test_an_upgrade_or_a_purchase_is_never_a_drop(self) -> None:
        owned = [OwnedWorkspace(IDS[0], "basic", "external_billing", BASE), _ws(2), _ws(3)]
        assert not is_allowance_drop(owned, 0, SlotChange(IDS[0], "free", "admin_grant"))
        owned = [OwnedWorkspace(IDS[0], "pro", "external_billing", BASE), _ws(2)]
        assert not is_allowance_drop(owned, 0, SlotChange(IDS[0], "basic", "external_billing"))

    def test_same_plan_repush_is_not_a_drop(self) -> None:
        owned = [OwnedWorkspace(IDS[0], "pro", "external_billing", BASE), _ws(2)]
        assert not is_allowance_drop(owned, 0, SlotChange(IDS[0], "pro", "external_billing"))

    def test_no_change_is_never_a_drop(self) -> None:
        assert not is_allowance_drop([_ws(0), _ws(2)], 0, None)

    def test_bonus_reduction_is_a_drop_and_an_increase_is_not(self) -> None:
        owned = [_ws(0), _ws(2), _ws(3)]
        assert is_allowance_drop(owned, 0, SlotChange(previous_slot_bonus=2))
        assert not is_allowance_drop(owned, 2, SlotChange(previous_slot_bonus=0))

    def test_bonus_reduction_covered_by_contracts_is_a_drop(self) -> None:
        owned = [_ws(0, contracted=True), _ws(3, contracted=True)]
        # cap 1 + 1 bonus = 2 before, 1 after; two contracts keep the allowance
        # 2. The bonus went down and the allowance did not rise: a drop, which
        # cuts only what exceeds the allowance (here nothing).
        assert is_allowance_drop(owned, 0, SlotChange(previous_slot_bonus=1))

    def test_a_change_to_a_workspace_no_longer_owned_is_ignored(self) -> None:
        owned = [_ws(0), _ws(2)]
        assert not is_allowance_drop(owned, 0, SlotChange(NOT_OWNED, "promax", "external_billing"))


class TestNonDecreasingChange:
    @pytest.mark.parametrize(
        ("old", "new", "expected"),
        [
            (("free", "admin_grant"), ("basic", "external_billing"), True),
            (("free", "external_billing"), ("promax", "external_billing"), True),
            (("pro", "external_billing"), ("pro", "external_billing"), True),
            (("free", "admin_grant"), ("free", "external_billing"), True),
            (("basic", "admin_grant"), ("pro", "admin_grant"), True),
            # Lower grant.
            (("promax", "external_billing"), ("free", "external_billing"), False),
            (("pro", "admin_grant"), ("basic", "admin_grant"), False),
            # Stopped being under contract, same grant.
            (("pro", "external_billing"), ("pro", "admin_grant"), False),
            (("basic", "external_billing"), ("free", "external_billing"), False),
        ],
    )
    def test_grant_not_lower_and_contract_not_ended(self, old, new, expected) -> None:
        assert change_is_non_decreasing(*old, *new) is expected

    def test_unknown_plan_names_rank_as_the_lowest_tier(self) -> None:
        assert change_is_non_decreasing("legacy", "admin_grant", "free", "admin_grant")
        assert not change_is_non_decreasing("pro", "admin_grant", "legacy", "admin_grant")
