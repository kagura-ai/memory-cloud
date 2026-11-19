"""The transition's decision on pure inputs (#2918): ``plan_transition``.

``reconcile_workspace_slots`` reads the owner's state, asks ``plan_transition``
which rows to clear and which to write, and writes them. A workspace the event
changed that still holds a suspension row counts as suspended going in (it
was not usable before the event), so a suspended Free workspace bought as Basic
swaps out an active one instead of joining the active set for free.

An event that ends a paid contract, lowers a plan's owned-workspace grant or
lowers the slot bonus, and does not raise the allowance, is a drop: it cuts the
active set to the allowance. Nothing else suspends.

The property test drives two billing events on different workspaces of one
owner, in both orders, from every state within the allowance on 2-4
workspaces, and asserts the final number of active workspaces does not depend
on the order, and that after every event that can lower the allowance and does
not raise it no more workspaces are active than the allowance.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from services.workspace_slots import (
    OwnedWorkspace,
    SlotChange,
    billing_contract_active,
    change_is_non_decreasing,
    plan_transition,
    slot_allowance,
)

B = datetime(2026, 1, 1)


def U(i: int) -> UUID:
    return UUID(int=i)


def _owned(world: dict[int, tuple[str, str]], rows: set[UUID]) -> list[OwnedWorkspace]:
    """As ``_read_owner`` builds it: suspended = own row and not under contract."""
    return [
        OwnedWorkspace(
            U(i),
            plan,
            source,
            B + timedelta(days=i),
            suspended=U(i) in rows and not billing_contract_active(plan, source),
        )
        for i, (plan, source) in world.items()
    ]


def _event(world, rows, wid, new, *, current=U(1), allow_suspend=True):
    """Apply one plan change of ``wid`` and its transition; returns the new state."""
    previous = world[wid]
    world = {**world, wid: new}
    decision = plan_transition(
        _owned(world, rows),
        rows,
        0,
        change=SlotChange(U(wid), *previous),
        allow_suspend=allow_suspend,
        keep_ids=(),
        current_workspace_id=current,
    )
    rows = (set(rows) - set(decision.clear)) | set(decision.suspend)
    return world, rows, decision


def _active(world, rows) -> set[int]:
    return {i for i, (p, s) in world.items() if U(i) not in rows or billing_contract_active(p, s)}


class TestChangedWorkspaceHoldingARow:
    def test_a_suspended_free_workspace_bought_as_basic_swaps_out_an_active_one(self) -> None:
        world = {1: ("free", "admin_grant"), 2: ("free", "admin_grant")}
        rows = {U(2)}
        world, rows, decision = _event(world, rows, 2, ("basic", "external_billing"))
        assert decision.drop is False
        assert decision.resumed == (U(2),)
        assert decision.suspend == (U(1),)
        assert _active(world, rows) == {2}
        # The Basic contract ends later: a drop (the allowance stays 1), and
        # one stays active.
        world, rows, decision = _event(world, rows, 2, ("free", "external_billing"))
        assert decision.drop is True
        assert _active(world, rows) == {2}

    def test_a_deferred_purchase_that_ends_leaves_one_active(self) -> None:
        """Allowance 1: A active, B suspended. B is bought as Basic while the
        owner lock is busy, so only the resume runs (both active). When that
        Basic ends the allowance stays 1, and the excess is suspended."""
        world = {1: ("free", "admin_grant"), 2: ("free", "admin_grant")}
        rows = {U(2)}
        world, rows, decision = _event(
            world, rows, 2, ("basic", "external_billing"), allow_suspend=False
        )
        assert decision.suspend == ()
        assert _active(world, rows) == {1, 2}
        world, rows, decision = _event(world, rows, 2, ("free", "external_billing"))
        assert decision.drop is True
        assert _active(world, rows) == {1}

    def test_on_a_resume_only_path_it_resumes_and_suspends_nothing(self) -> None:
        world = {1: ("free", "admin_grant"), 2: ("basic", "external_billing")}
        decision = plan_transition(
            _owned(world, {U(2)}),
            {U(2)},
            0,
            change=SlotChange(U(2), "free", "admin_grant"),
            allow_suspend=False,
            keep_ids=(),
            current_workspace_id=U(1),
        )
        assert (decision.resumed, decision.suspend) == ((U(2),), ())
        assert decision.clear == (U(2),)

    def test_a_cancellation_and_a_basic_purchase_end_alike_in_both_orders(self) -> None:
        start = {
            1: ("pro", "external_billing"),
            2: ("free", "admin_grant"),
            3: ("free", "admin_grant"),
        }
        cancel = (1, ("free", "external_billing"))
        buy = (2, ("basic", "external_billing"))
        results = []
        for order in ((cancel, buy), (buy, cancel)):
            world, rows = dict(start), set()
            for wid, new in order:
                world, rows, _ = _event(world, rows, wid, new)
            results.append(_active(world, rows))
        assert results[0] == results[1] == {2}


class TestOwnersOverTheAllowanceFromBefore:
    """Three Free and a billed Basic, all active: allowance 1, three over."""

    START = {
        1: ("free", "admin_grant"),
        2: ("free", "admin_grant"),
        3: ("free", "admin_grant"),
        4: ("basic", "external_billing"),
    }

    def test_the_end_of_a_paid_contract_cuts_them_to_the_allowance(self) -> None:
        world, rows, decision = _event(dict(self.START), set(), 4, ("free", "external_billing"))
        assert decision.drop is True
        assert _active(world, rows) == {1}

    @pytest.mark.parametrize(
        "wid, new",
        [
            (1, ("basic", "external_billing")),  # a purchase
            (4, ("pro", "external_billing")),  # an upgrade
            (4, ("basic", "external_billing")),  # a same-plan re-push
        ],
    )
    def test_a_non_decreasing_change_leaves_them_alone(self, wid, new) -> None:
        world, rows, decision = _event(dict(self.START), set(), wid, new)
        assert decision.drop is False
        assert decision.suspend == ()
        assert rows == set()

    def test_a_selection_save_or_a_delete_leaves_them_alone(self) -> None:
        owned = _owned(dict(self.START), set())
        saved = plan_transition(
            owned,
            set(),
            0,
            change=None,
            allow_suspend=True,
            keep_ids=(U(2),),
            current_workspace_id=U(1),
        )
        deleted = plan_transition(
            owned[1:],
            set(),
            0,
            change=None,
            allow_suspend=False,
            keep_ids=(),
            current_workspace_id=U(1),
        )
        assert (saved.drop, saved.suspend) == (False, ())
        assert (deleted.drop, deleted.suspend) == (False, ())


PLANS = [
    ("free", "admin_grant"),
    ("basic", "external_billing"),
    ("pro", "external_billing"),
    ("promax", "external_billing"),
    ("pro", "admin_grant"),
    ("free", "external_billing"),
]


def _cut_to_allowance(before: dict, wid: int, new: tuple[str, str], world, rows) -> bool:
    """After an event that can lower the allowance and does not raise it, no
    more workspaces are active than the allowance."""
    allow_before = slot_allowance(_owned(before, set()), 0).allowance
    allow_after = slot_allowance(_owned(world, set()), 0).allowance
    can_lower = not change_is_non_decreasing(*before[wid], *new)
    if can_lower and allow_after <= allow_before:
        return len(_active(world, rows)) <= allow_after
    return True


def _checked(world, rows, wid, new):
    after, after_rows, decision = _event(world, rows, wid, new)
    assert _cut_to_allowance(world, wid, new, after, after_rows), (world, rows, wid, new)
    return after, after_rows, decision


def test_the_active_count_never_depends_on_the_order_of_two_events() -> None:
    pairs = 0
    for n in (2, 3, 4):
        for start in itertools.product(range(len(PLANS)), repeat=n):
            world = {i + 1: PLANS[k] for i, k in enumerate(start)}
            if len(world) > slot_allowance(_owned(world, set()), 0).allowance:
                continue  # owners over the allowance from before are out of scope
            for a, b in itertools.combinations(world, 2):
                for na, nb in itertools.product(PLANS, repeat=2):
                    if na == world[a] or nb == world[b]:
                        continue
                    w1, r1, _ = _checked(*_checked(world, set(), a, na)[:2], b, nb)
                    w2, r2, _ = _checked(*_checked(world, set(), b, nb)[:2], a, na)
                    pairs += 1
                    assert len(_active(w1, r1)) == len(_active(w2, r2)), (world, a, na, b, nb)
    assert pairs == 118_000
