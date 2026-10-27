"""#2275: per-key provenance of ``details`` — ``plan_details_erasure``.

Pure: rows are ``DetailsEdit``s (the ``updated`` rows of the candidate
memories, in any order), ``live`` maps each memory to its live ``details``
and author. Each prior is the whole dict the edit overwrote.
"""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from services.details_provenance import (
    UNKNOWN,
    DetailsEdit,
    DetailsErasurePlan,
    KeyRevert,
    PriorStrip,
    details_key_digest,
    plan_details_erasure,
    replay,
)

S = "erased-sub"
P_S = "5" * 64
AUTH = "the-author"
B = "member-b"
C = "member-c"


def _edit(
    memory_id: UUID,
    version: int,
    actor: str,
    prior: object = None,
    *,
    erased: bool = False,
    writer: str = "api",
    row_id: int | None = None,
    restored_from: int | None = None,
    details_changed: bool = True,
) -> DetailsEdit:
    return DetailsEdit(
        memory_id=memory_id,
        version=version,
        id=row_id if row_id is not None else version * 10,
        actor_user_id=actor,
        writer=writer,
        details=None if erased or not details_changed else prior,
        erased=erased,
        restored_from=restored_from,
        details_changed=details_changed,
    )


def _rollback(
    memory_id: UUID, version: int, actor: str, prior: object, restored_from: int | None = None
) -> DetailsEdit:
    return _edit(memory_id, version, actor, prior, writer="rollback", restored_from=restored_from)


def _old_erasure(memory_id: UUID, version: int, actor: str, restored_from: int) -> DetailsEdit:
    """The row an erasure before v0.129.0 wrote: ``erased`` + ``restored_from``."""
    return _edit(
        memory_id, version, actor, erased=True, writer="erasure", restored_from=restored_from
    )


def _plan(
    rows: list[DetailsEdit],
    live: Any,
    subject: str = S,
    *,
    memory_id: UUID | None = None,
    author: str = AUTH,
    erased_actors: frozenset[str] = frozenset(),
) -> DetailsErasurePlan:
    m = memory_id if memory_id is not None else rows[0].memory_id
    return plan_details_erasure(rows, {m: (live, author)}, subject, erased_actors=erased_actors)


def _apply(
    rows: list[DetailsEdit],
    live: dict[UUID, tuple[Any, str]],
    plan: DetailsErasurePlan,
    *,
    subject: str,
    pseudonym: str,
) -> tuple[list[DetailsEdit], dict[UUID, tuple[Any, str]]]:
    """What ``erase_subject_details`` + the pseudonymization write, in memory."""
    strips = {s.row_id: s.keys for s in plan.strips}
    out: list[DetailsEdit] = []
    for row in rows:
        if row.id in strips and isinstance(row.details, dict):
            keys = strips[row.id]
            row = replace(
                row,
                details={k: v for k, v in row.details.items() if k not in keys},
                erased_keys=row.erased_keys | {details_key_digest(k) for k in keys},
            )
        if row.actor_user_id == subject:
            row = replace(row, actor_user_id=pseudonym)
        out.append(row)
    after = dict(live)
    for revert in plan.reverts:
        top = max(r.version for r in out if r.memory_id == revert.memory_id)
        out.append(
            DetailsEdit(
                memory_id=revert.memory_id,
                version=top + 1,
                id=10_000 + len(out),
                actor_user_id=pseudonym,
                writer="erasure",
                details=revert.prior,
                erased=False,
                erased_keys=frozenset(revert.erased_keys),
                restored_from=revert.from_version,
            )
        )
        after[revert.memory_id] = (revert.result, live[revert.memory_id][1])
    return out, after


def _worked_example() -> tuple[UUID, list[DetailsEdit], dict[str, Any]]:
    """gate1's table: AUTH {a:1}; S adds s, t; B rewrites a; C rewrites t, adds c."""
    m = uuid4()
    rows = [
        _edit(m, 2, S, {"a": 1}),
        _edit(m, 3, B, {"a": 1, "s": "x", "t": "y"}),
        _edit(m, 4, C, {"a": 2, "s": "x", "t": "y"}),
    ]
    return m, rows, {"a": 2, "s": "x", "t": "z", "c": True}


def test_the_worked_example_reverts_the_carried_key_and_strips_per_key() -> None:
    m, rows, live = _worked_example()
    plan = _plan(rows, live)
    (revert,) = plan.reverts
    assert revert.memory_id == m
    assert revert.reverts == (KeyRevert("s", None, False, False, 2),)
    assert revert.from_version == 2
    assert revert.result == {"a": 2, "t": "z", "c": True}
    assert revert.prior == {"a": 2, "t": "z", "c": True}
    # row 2's prior (the author's {a:1}) is untouched; rows 3 and 4 lose s and t
    assert plan.strips == (PriorStrip(30, ("s", "t")), PriorStrip(40, ("s", "t")))
    assert plan.keys_ambiguous == 0
    assert plan.memory_ids == (m,)


def test_a_carried_key_goes_back_to_its_value_below_the_subjects_write() -> None:
    m = uuid4()
    rows = [_edit(m, 2, S, {"a": 1, "s": 1}), _edit(m, 3, B, {"a": 1, "s": 2})]
    plan = _plan(rows, {"a": 3, "s": 2})
    (revert,) = plan.reverts
    assert revert.reverts == (KeyRevert("s", 1, True, False, 2),)
    assert revert.result == {"a": 3, "s": 1}
    assert plan.strips == (PriorStrip(30, ("s",)),)


def test_a_key_deleted_and_re_added_with_the_same_value_is_the_re_adders() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, S, {}),
        _edit(m, 3, B, {"k": 1}),  # B removed S's k
        _edit(m, 4, C, {}),  # C added k back, same value
    ]
    plan = _plan(rows, {"k": 1})
    assert plan.reverts == ()
    assert plan.strips == (PriorStrip(30, ("k",)),)


def test_the_subjects_consecutive_edits_go_back_to_the_value_before_the_run() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, AUTH, {"by": "creation"}),
        _edit(m, 3, S, {"by": "author"}),
        _edit(m, 5, S, {"by": "s-1"}),
        _edit(m, 7, S, {"by": "s-2"}),
    ]
    plan = _plan(rows, {"by": "s-3"})
    (revert,) = plan.reverts
    assert revert.reverts == (KeyRevert("by", "author", True, False, 3),)
    assert revert.from_version == 3
    assert plan.strips == (PriorStrip(50, ("by",)), PriorStrip(70, ("by",)))


def test_a_chain_of_erased_actors_is_walked_past_to_the_survivors_value() -> None:
    m = uuid4()
    s1, s2 = "1" * 64, "2" * 64
    rows = [
        _edit(m, 2, s1, {"by": "creation"}),
        _edit(m, 3, B, {"by": "s1"}),
        _edit(m, 4, s2, {"by": "b"}),
        _edit(m, 5, s1, {"by": "s2"}),
    ]
    live = {"by": "s1-again"}
    plan = _plan(rows, live, s1, erased_actors=frozenset({s2}))
    (revert,) = plan.reverts
    assert revert.reverts == (KeyRevert("by", "b", True, False, 4),)
    # s1's own states: the prior of B's edit (row 30) holds s1's value
    assert plan.strips == (PriorStrip(30, ("by",)),)
    # s2 first: the live key is s1's — left to s1's erasure; s2's value leaves row 50
    other = _plan(rows, live, s2, erased_actors=frozenset({s1}))
    assert other.reverts == ()
    assert other.strips == (PriorStrip(50, ("by",)),)
    # without the set, s2 is a surviving writer and their value comes back
    assert _plan(rows, live, s1).reverts[0].reverts[0].target == "s2"


def test_a_rollback_that_put_the_subjects_value_back_leaves_it_theirs() -> None:
    m = uuid4()
    for requester in (B, AUTH, S):
        rows = [
            _edit(m, 2, S, {}),
            _edit(m, 3, B, {"k": 1}),
            _rollback(m, 4, requester, {"k": 2}, restored_from=3),
        ]
        plan = _plan(rows, {"k": 1})
        (revert,) = plan.reverts
        assert revert.reverts == (KeyRevert("k", None, False, False, 2),), requester
        assert revert.result == {}
        assert plan.strips == (PriorStrip(30, ("k",)),), requester


def test_the_subjects_rollback_of_another_session_is_not_their_writing() -> None:
    m = uuid4()
    rows = [_edit(m, 2, B, {"by": "author"}), _rollback(m, 3, S, {"by": "b"}, restored_from=2)]
    assert _plan(rows, {"by": "author"}) == DetailsErasurePlan(memory_ids=(m,))


def test_a_rollback_row_without_restored_from_is_attributed_by_value() -> None:
    """A session rollback from before #2266: the value it put back is the
    newest earlier state's that held it."""
    m = uuid4()
    rows = [
        _edit(m, 2, S, {"a": 1}),
        _edit(m, 3, B, {"a": 1, "s": 1}),
        _rollback(m, 4, AUTH, {"a": 2, "s": 1}),
    ]
    plan = _plan(rows, {"a": 1, "s": 1})
    (revert,) = plan.reverts
    assert revert.reverts == (KeyRevert("s", None, False, False, 2),)
    assert revert.result == {"a": 1}


def test_a_session_that_re_wrote_an_earlier_value_misleads_only_the_legacy_match() -> None:
    """The documented residual: without ``restored_from``, a rolled-back
    session that itself wrote the value it is rolled back to takes the key."""
    m = uuid4()
    rows = [
        _edit(m, 2, S, {"k": 0}),
        _edit(m, 3, B, {"k": 1}),
        _edit(m, 4, B, {"k": 2}),
        _edit(m, 5, B, {"k": 1}),
    ]
    legacy = [*rows, _rollback(m, 6, AUTH, {"k": 3})]
    assert _plan(legacy, {"k": 1}).reverts == ()
    stamped = [*rows, _rollback(m, 6, AUTH, {"k": 3}, restored_from=3)]
    (revert,) = _plan(stamped, {"k": 1}).reverts
    assert revert.reverts == (KeyRevert("k", 0, True, False, 2),)


def test_an_old_erasure_row_on_top_belongs_to_the_survivor_below() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, AUTH, {"by": "creation"}),
        _edit(m, 3, P_S, {"by": "author"}),
        _old_erasure(m, 4, P_S, restored_from=3),
    ]
    run = replay(
        sorted(rows, key=lambda r: r.version), {"by": "author"}, AUTH, subject=P_S, erased={P_S}
    )
    assert run.states[2] is UNKNOWN
    assert run.writers[3] is not None and run.writers[3]["by"].actor == AUTH
    plan = _plan(rows, {"by": "author"}, P_S)
    assert plan.reverts == () and plan.strips == () and plan.keys_ambiguous == 0


def test_a_second_erasure_over_a_new_erasure_row_reverts_only_its_keys() -> None:
    m, rows, live = _worked_example()
    first = _plan(rows, live)
    rows, after = _apply(rows, {m: (live, AUTH)}, first, subject=S, pseudonym=P_S)
    plan = plan_details_erasure(rows, after, B, erased_actors={P_S})
    (revert,) = plan.reverts
    assert revert.reverts == (KeyRevert("a", 1, True, False, 3),)
    assert revert.result == {"a": 1, "t": "z", "c": True}
    erasure_row = rows[-1].id
    assert plan.strips == (PriorStrip(40, ("a",)), PriorStrip(erasure_row, ("a",)))
    # neither pseudonym finds anything once both ran
    rows, after = _apply(rows, after, plan, subject=B, pseudonym="b" * 64)
    for subject in (P_S, "b" * 64):
        again = plan_details_erasure(rows, after, subject, erased_actors={P_S, "b" * 64})
        assert (again.reverts, again.strips, again.keys_ambiguous) == ((), (), 0)


def test_a_stripped_state_below_the_subjects_write_makes_the_key_unavailable() -> None:
    m = uuid4()
    rows = [_edit(m, 2, B, {"a": 1}), _edit(m, 3, S, erased=True)]
    plan = _plan(rows, {"a": 1, "s": 1})
    (revert,) = plan.reverts
    assert revert.reverts == (KeyRevert("s", None, False, True, 3),)
    assert revert.unavailable
    assert revert.result == {"a": 1}


def test_a_pre_v0129_stripped_state_with_a_survivors_edit_above_is_ambiguous() -> None:
    """gate1's re-run variant: p_S's written state was stripped whole; B's edit
    above it may carry p_S's keys or be B's own — kept and counted."""
    m = uuid4()
    rows = [_edit(m, 2, P_S, {"a": 1}), _edit(m, 3, B, erased=True)]
    plan = _plan(rows, {"a": 2, "s": "x"}, P_S)
    assert plan.reverts == ()
    assert plan.strips == ()
    assert plan.keys_ambiguous == 2
    # a key B carried unchanged from the last known state is the author's, not ambiguous
    assert _plan(rows, {"a": 1, "s": "x"}, P_S).keys_ambiguous == 1
    # the stripped state is another erased member's: nothing of the subject's below
    other = [_edit(m, 2, "x" * 64, {"a": 1}), _edit(m, 3, B, erased=True)]
    assert _plan(other, {"a": 2, "s": "x"}, P_S).keys_ambiguous == 0


def test_a_stripped_state_an_erased_member_wrote_is_walked_past() -> None:
    m = uuid4()
    x = "x" * 64
    rows = [_edit(m, 2, x, {"a": 1}), _edit(m, 3, S, erased=True)]
    plan = _plan(rows, {"a": 5, "s": 1}, erased_actors=frozenset({x}))
    (revert,) = plan.reverts
    assert revert.reverts == (
        KeyRevert("a", 1, True, False, 2),
        KeyRevert("s", None, False, False, 2),
    )
    assert revert.result == {"a": 1}
    assert plan.keys_ambiguous == 0


def test_an_empty_result_is_null_only_over_a_null_value() -> None:
    m = uuid4()
    (revert,) = _plan([_edit(m, 2, S, None)], {"s": 1}).reverts
    assert revert.result is None
    (revert,) = _plan([_edit(m, 2, S, {})], {"s": 1}).reverts
    assert revert.result == {}


def test_deletions_stand() -> None:
    m = uuid4()
    rows = [_edit(m, 2, S, {"a": 1, "b": 2})]
    assert _plan(rows, {"a": 1}).reverts == ()
    (revert,) = _plan(rows, {"a": 1, "s": 1}).reverts
    assert revert.result == {"a": 1}  # b stays removed


def test_rows_that_changed_no_details_are_ignored() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, AUTH, {"by": "creation"}),
        _edit(m, 3, S, details_changed=False),
        _edit(m, 4, S, {"by": "author"}),
        _edit(m, 5, B, details_changed=False),
    ]
    (revert,) = _plan(rows, {"by": "s"}).reverts
    assert revert.reverts == (KeyRevert("by", "author", True, False, 4),)
    assert _plan(rows, {"by": "s"}, B).reverts == ()


def test_a_null_live_value_reverts_nothing_but_strips() -> None:
    m = uuid4()
    rows = [_edit(m, 2, S, {"a": 1}), _edit(m, 3, B, {"a": 1, "s": 1})]
    plan = _plan(rows, None)
    assert plan.reverts == ()
    assert plan.strips == (PriorStrip(30, ("s",)),)


def test_a_value_a_put_back_synthesized_is_nobodys() -> None:
    """The trigger window an erasure merged into a time memory matches no
    earlier state: unattributed, never reverted."""
    m = uuid4()
    rows = [
        _edit(m, 2, S, {"trigger": {"text": "author"}}),
        _edit(m, 3, P_S, {}, writer="erasure", restored_from=2),
    ]
    live = {"trigger": {"from": "2026-01-01T00:00:00Z"}}
    run = replay(rows, live, AUTH, subject=P_S, erased={P_S})
    assert run.writers[2] is not None and run.writers[2]["trigger"].actor is None
    for subject in (P_S, B, AUTH):
        assert _plan(rows, live, subject).reverts == ()


def test_same_version_rows_order_by_id() -> None:
    m = uuid4()
    rows = [_edit(m, 3, S, {"k": 1}, row_id=7), _edit(m, 3, B, {"k": 0}, row_id=5)]
    (revert,) = _plan(rows, {"k": 2}).reverts
    assert revert.reverts == (KeyRevert("k", 1, True, False, 3),)


def test_output_is_ordered_and_insensitive_to_input_order() -> None:
    ids = sorted(uuid4() for _ in range(4))
    rows: list[DetailsEdit] = []
    live: dict[UUID, tuple[Any, str]] = {}
    for n, m in enumerate(ids):
        rows += [
            _edit(m, 2, S, {"a": n}, row_id=100 - n * 10),
            _edit(m, 3, B, {"a": n, "s": n}, row_id=101 - n * 10),
        ]
        live[m] = ({"a": n, "s": n}, AUTH)
    expected = plan_details_erasure(rows, live, S)
    assert [r.memory_id for r in expected.reverts] == ids
    assert [s.row_id for s in expected.strips] == sorted(s.row_id for s in expected.strips)
    shuffled = rows[:]
    random.Random(2275).shuffle(shuffled)
    assert plan_details_erasure(shuffled, live, S) == expected
    assert plan_details_erasure(rows, live, "nobody").reverts == ()


def test_memories_the_subject_authored_or_that_are_gone_are_skipped() -> None:
    m = uuid4()
    rows = [_edit(m, 2, S, {"a": 1})]
    assert plan_details_erasure(rows, {m: ({"a": 2}, S)}, S) == DetailsErasurePlan()
    assert plan_details_erasure(rows, {}, S) == DetailsErasurePlan()


def test_a_re_run_over_the_result_plans_nothing() -> None:
    m, rows, live = _worked_example()
    n = uuid4()
    more = [  # row ids are unique across memories, as in the table
        _edit(n, 2, S, {"a": 1}, row_id=21),
        _edit(n, 3, B, {"a": 1, "s": 1}, row_id=31),
        _rollback(n, 4, AUTH, {"a": 2, "s": 1}, restored_from=3),
        _edit(n, 5, S, {"a": 1, "s": 1}, row_id=51),
    ]
    more[2] = replace(more[2], id=41)
    state = {m: (live, AUTH), n: ({"a": 1, "s": 2, "t": 3}, AUTH)}
    plan = plan_details_erasure([*rows, *more], state, S)
    assert len(plan.reverts) == 2
    after_rows, after = _apply([*rows, *more], state, plan, subject=S, pseudonym=P_S)
    again = plan_details_erasure(after_rows, after, P_S)
    assert (again.reverts, again.strips, again.keys_ambiguous) == ((), (), 0)


def _erase_in_order(
    rows: list[DetailsEdit], live: Any, order: list[str]
) -> tuple[Any, list[DetailsEdit]]:
    m = rows[0].memory_id
    state = {m: (live, AUTH)}
    done: list[str] = []
    for subject in order:
        pseudonym = subject * 64 if len(subject) == 1 else subject
        plan = plan_details_erasure(
            rows, state, subject, erased_actors=frozenset(p * 64 for p in done)
        )
        rows, state = _apply(rows, state, plan, subject=subject, pseudonym=pseudonym)
        done.append(subject)
    return state[m][0], rows


def test_the_result_does_not_depend_on_which_erasure_runs_first() -> None:
    """Review of #2275: a key stripped from a prior must not read as a removal
    in a later erasure's walk (the digest marker), and a put-back state's
    stripped value comes from ``pre(r)``, not from the state it replaced."""
    m = uuid4()
    s_, t_ = "S", "T"
    # A {a, c}; S rewrites both; B rewrites a; B removes a; T rewrites c
    carried = [
        _edit(m, 2, s_, {"a": "a0", "c": "c0"}),
        _edit(m, 3, B, {"a": "aS", "c": "cS"}),
        _edit(m, 4, B, {"a": "aB", "c": "cS"}),
        _edit(m, 5, t_, {"c": "cS"}),
    ]
    # B wrote, S over it, C over S, B rolled C's session back, B wrote again
    rolled = [
        _edit(m, 2, "C", {"b": "b0"}),
        _edit(m, 3, s_, {"b": "bC"}),
        _edit(m, 4, "C", {"b": "bS", "c": "cS"}),
        _rollback(m, 5, "B", {"b": "bC2", "c": "cS"}, restored_from=4),
        _edit(m, 6, "B", {"b": "bS", "c": "cS"}),
    ]
    for rows, live, erased, expected in (
        (carried, {"c": "cT"}, ["S", "T"], {"c": "c0"}),
        (rolled, {"b": "bB", "c": "cS", "a": "aB"}, ["S", "B"], {"b": "bC"}),
    ):
        results = [_erase_in_order(rows, live, order)[0] for order in (erased, erased[::-1])]
        assert results == [expected, expected], rows


def test_a_removal_stands_whichever_erasure_runs_first() -> None:
    """S removed c and wrote it again later; T's carry in between does not
    matter: the walk stops at S's removal, in either order."""
    m = uuid4()
    rows = [
        _edit(m, 2, "T", {"b": "b0", "c": "c0"}),
        _edit(m, 3, "S", {"b": "b0", "c": "c0", "t": "t1"}),
        _edit(m, 4, "T", {"b": "b0", "t": "t1"}),
        _edit(m, 5, "S", {"b": "b0", "t": "t1"}),
    ]
    live = {"b": "b0", "t": "t1", "c": "cS"}
    for order in (["S", "T"], ["T", "S"]):
        assert _erase_in_order(rows, live, order)[0] == {"b": "b0"}, order


def test_a_put_back_over_a_stripped_state_is_ambiguous_only_above_the_subjects_edit() -> None:
    """The stripped state a put-back row wrote may hold the subject's value
    only when the subject edited below it."""
    m = uuid4()
    x = "x" * 64
    put_back = _old_erasure(m, 3, x, restored_from=2)
    unrelated = [
        _edit(m, 2, x, {"a": 1}),
        put_back,
        _edit(m, 4, B, erased=True),
        _edit(m, 5, P_S, {"a": 1, "b": 2}),
    ]
    # P_S edited only above the stripped state: nothing of theirs can be in it
    assert _plan(unrelated, {"a": 1, "b": 2, "s": 3}, P_S).keys_ambiguous == 0
    # P_S wrote ``a`` below it: ``s`` (absent from the last known state) may
    # be theirs; ``a``, unchanged since, is theirs and goes
    below = [_edit(m, 1, P_S, {}, row_id=5), *unrelated[:3]]
    plan = _plan(below, {"a": 1, "s": 3}, P_S)
    assert plan.keys_ambiguous == 1
    assert [r.key for r in plan.reverts[0].reverts] == ["a"]
