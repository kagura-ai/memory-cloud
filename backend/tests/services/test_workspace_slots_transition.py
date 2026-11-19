"""The transition's decision on pure inputs (#2918): ``plan_transition``.

``reconcile_workspace_slots`` reads the owner's state, asks ``plan_transition``
which rows to clear and which to write, and writes them. A workspace the event
changed that still holds a suspension row counts as suspended going in (it
was not usable before the event), so a suspended Free workspace bought as Basic
swaps out an active one instead of joining the active set for free.

The property test drives two billing events on different workspaces of one
owner, in both orders, from every state within the allowance on 2-4
workspaces, and asserts the final number of active workspaces does not depend
on the order.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from time import monotonic
from uuid import UUID

from services.workspace_slots import (
    OwnedWorkspace,
    SlotChange,
    billing_contract_active,
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
        # The Basic contract ends later: not a drop, and one stays active.
        world, rows, decision = _event(world, rows, 2, ("free", "external_billing"))
        assert decision.drop is False
        assert _active(world, rows) == {2}

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


PLANS = [
    ("free", "admin_grant"),
    ("basic", "external_billing"),
    ("pro", "external_billing"),
    ("promax", "external_billing"),
    ("pro", "admin_grant"),
    ("free", "external_billing"),
]


def test_the_active_count_never_depends_on_the_order_of_two_events() -> None:
    started = monotonic()
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
                    w1, r1, _ = _event(*_event(world, set(), a, na)[:2], b, nb)
                    w2, r2, _ = _event(*_event(world, set(), b, nb)[:2], a, na)
                    pairs += 1
                    assert len(_active(w1, r1)) == len(_active(w2, r2)), (world, a, na, b, nb)
    assert pairs == 118_000
    assert monotonic() - started < 60
