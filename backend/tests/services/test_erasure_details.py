"""#2244 / #2275: the details an erased editor wrote, per key.

The walk scenarios of the whole-value restore (#2244, #2266, #2268), read
per top-level key through ``plan_details_erasure``: rows are ``DetailsEdit``s
(the ``updated`` rows of the candidate memories, in any order), and each
memory's live ``details`` and author are given. Then the per-key strip and
the restore row, compiled.
"""

from __future__ import annotations

import random
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from services.details_provenance import (
    DetailsErasurePlan,
    KeyRevert,
    MemoryRevert,
    PriorStrip,
    plan_details_erasure,
)
from services.erasure_details import DetailsEdit

SUBJECT = "erased-sub"
AUTHOR = "the-author"
OTHER = "other-member"
PSEUDONYM = "a" * 64


def _edit(
    memory_id: UUID,
    version: int,
    actor: str,
    details: object = None,
    *,
    erased: bool = False,
    writer: str = "api",
    row_id: int | None = None,
    undone_by: int | None = None,
    restored_from: int | None = None,
    details_changed: bool = True,
) -> DetailsEdit:
    return DetailsEdit(
        memory_id=memory_id,
        version=version,
        id=row_id if row_id is not None else version * 10,
        actor_user_id=actor,
        writer=writer,
        details=None if erased or not details_changed else details,
        erased=erased,
        undone_by=undone_by,
        restored_from=restored_from,
        details_changed=details_changed,
    )


def _other_field(memory_id: UUID, version: int, actor: str) -> DetailsEdit:
    """An ``updated`` row that changed no ``details``."""
    return _edit(memory_id, version, actor, details_changed=False)


def _erasure(memory_id: UUID, version: int, actor: str, restored_from: int) -> DetailsEdit:
    """The row ``restore_editor_details`` wrote before v0.129.0 (``erased``)."""
    return _edit(
        memory_id, version, actor, erased=True, writer="erasure", restored_from=restored_from
    )


def _rollback(
    memory_id: UUID, version: int, actor: str, undid: object, restored_from: int | None = None
) -> DetailsEdit:
    """A session rollback's put-back row: its prior is the value it undid
    (and, since #2266, the lowest version of the block it reverted)."""
    return _edit(memory_id, version, actor, undid, writer="rollback", restored_from=restored_from)


def _reverts(
    rows: list[DetailsEdit],
    live: Any,
    subject: str = SUBJECT,
    *,
    author: str = AUTHOR,
    erased_actors: frozenset[str] = frozenset(),
) -> list[tuple[Any, int, bool]]:
    """Per memory: (the value written, ``from_version``, unavailable)."""
    m = rows[0].memory_id
    plan = plan_details_erasure(rows, {m: (live, author)}, subject, erased_actors=erased_actors)
    return [(r.result, r.from_version, r.unavailable) for r in plan.reverts]


def _strips(rows: list[DetailsEdit], live: Any, subject: str = SUBJECT, **kwargs: Any) -> list[int]:
    m = rows[0].memory_id
    plan = plan_details_erasure(rows, {m: (live, AUTHOR)}, subject, **kwargs)
    return [s.row_id for s in plan.strips]


def test_latest_edit_by_the_subject_goes_back_to_the_previous_writers_value() -> None:
    m = uuid4()
    rows = [_edit(m, 2, AUTHOR, {"by": "creation"}), _edit(m, 4, SUBJECT, {"by": "author"})]
    assert _reverts(rows, {"by": "subject"}) == [({"by": "author"}, 4, False)]


def test_the_subjects_consecutive_edits_are_walked_past() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 3, SUBJECT, {"by": "author"}),
        _edit(m, 5, SUBJECT, {"by": "subject-1"}),
        _edit(m, 7, SUBJECT, {"by": "subject-2"}),
    ]
    assert _reverts(rows, {"by": "subject-3"}) == [({"by": "author"}, 3, False)]


def test_a_key_the_subject_removed_stays_removed() -> None:
    """#2275 behaviour change: the whole-value revert put the creation value
    back whole; per key, the subject's ``by`` goes and the ``at`` they
    removed stays removed (deletions stand)."""
    m = uuid4()
    rows = [_edit(m, 2, SUBJECT, {"at": "creation"}), _edit(m, 3, SUBJECT, {"by": "subject"})]
    assert _reverts(rows, {"by": "subject-2"}) == [({}, 2, False)]


def test_the_creation_value_may_be_null() -> None:
    m = uuid4()
    assert _reverts([_edit(m, 2, SUBJECT, None)], {"s": 1}) == [(None, 2, False)]


def test_a_later_rewrite_by_someone_else_means_no_restore() -> None:
    m = uuid4()
    rows = [_edit(m, 2, SUBJECT, {"by": "author"}), _edit(m, 3, OTHER, {"by": "subject"})]
    assert _reverts(rows, {"by": "other"}) == []
    assert _strips(rows, {"by": "other"}) == [30]


def test_a_later_whole_dict_edit_that_carried_the_key_is_not_a_rewrite() -> None:
    """#2275: OTHER changed ``a`` and sent the subject's ``s`` back as it was."""
    m = uuid4()
    rows = [_edit(m, 2, SUBJECT, {"a": 1}), _edit(m, 3, OTHER, {"a": 1, "s": 1})]
    assert _reverts(rows, {"a": 2, "s": 1}) == [({"a": 2}, 2, False)]


def test_an_erased_prior_gives_unavailable() -> None:
    """Double erasure: the subject's run sits on a state an earlier erasure
    stripped whole, written by someone not erased now: the key is removed."""
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 3, PSEUDONYM, {"by": "author"}),
        _edit(m, 4, SUBJECT, erased=True),
        _edit(m, 5, SUBJECT, {"by": "subject"}),
    ]
    assert _reverts(rows, {"by": "subject-2"}) == [(None, 4, True)]
    # with the stripped state's writer erased, the walk goes on below it
    assert _reverts(rows, {"by": "subject-2"}, erased_actors=frozenset({PSEUDONYM})) == [
        ({"by": "author"}, 3, False)
    ]


def test_an_erasure_row_on_top_is_not_the_subjects_writing() -> None:
    """Idempotency of a re-run by pseudonym (#2243): the value an erasure row
    put back belongs to the edit below the version it names."""
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 3, PSEUDONYM, {"by": "author"}),
        _erasure(m, 4, PSEUDONYM, restored_from=3),
    ]
    assert _reverts(rows, {"by": "author"}, PSEUDONYM) == []
    assert _strips(rows, {"by": "author"}, PSEUDONYM) == []


def test_an_erasure_row_below_ends_the_run() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, PSEUDONYM, {"by": "author"}),
        _erasure(m, 3, PSEUDONYM, restored_from=2),
        _edit(m, 4, SUBJECT, {"by": "author"}),
    ]
    assert _reverts(rows, {"by": "subject"}) == [({"by": "author"}, 4, False)]


def test_a_second_erasure_finds_the_value_the_first_one_put_back() -> None:
    """Review M2: A authors (dA); V edits (dV); U edits (dU). U's erasure puts
    dV back with ``restored_from=3``. V's erasure must see that dV is V's and
    put dA back; a re-run by either pseudonym afterwards finds nothing."""
    m = uuid4()
    p_u, p_v = "u" * 64, "v" * 64
    rows = [
        _edit(m, 2, "member-v", {"by": "author"}),
        _edit(m, 3, p_u, {"by": "v"}),
        _erasure(m, 4, p_u, restored_from=3),
    ]
    assert _reverts(rows, {"by": "v"}, "member-v") == [({"by": "author"}, 2, False)]
    after = [_edit(m, 2, p_v, {"by": "author"}), *rows[1:], _erasure(m, 5, p_v, restored_from=2)]
    assert _reverts(after, {"by": "author"}, p_v) == []
    assert _reverts(after, {"by": "author"}, p_u) == []


def test_the_subjects_rollback_of_another_session_is_not_their_writing() -> None:
    """Review M1: OTHER's session edits A's memory, the subject rolls it back."""
    m = uuid4()
    rows = [
        _edit(m, 2, OTHER, {"by": "author"}, undone_by=7),
        _rollback(m, 3, SUBJECT, {"by": "other"}),
    ]
    assert _reverts(rows, {"by": "author"}) == []
    assert _strips(rows, {"by": "author"}) == []


def test_a_rollback_over_the_subjects_value_leaves_the_subject_as_the_writer() -> None:
    """Review M1 variants: the subject wrote, OTHER's session wrote over it
    and was rolled back (a row from before #2266, no ``restored_from``) — by
    the subject or by the author. The subject's value is live and theirs."""
    m = uuid4()
    for requester in (SUBJECT, AUTHOR):
        rows = [
            _edit(m, 2, SUBJECT, {"by": "author"}),
            _edit(m, 3, OTHER, {"by": "subject"}, undone_by=7),
            _edit(m, 4, OTHER, {"by": "other-1"}, undone_by=7),
            _rollback(m, 5, requester, {"by": "other-2"}),
        ]
        assert _reverts(rows, {"by": "subject"}) == [({"by": "author"}, 2, False)], requester
        assert _strips(rows, {"by": "subject"}) == [30], requester


def test_a_rollback_over_the_subjects_value_with_restored_from() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, SUBJECT, {"by": "author"}),
        _edit(m, 3, OTHER, {"by": "subject"}),
        _rollback(m, 4, AUTHOR, {"by": "other"}, restored_from=3),
        _edit(m, 5, AUTHOR, {"by": "subject", "x": 1}),
    ]
    # the author's later edit carried the subject's value and added x
    assert _reverts(rows, {"by": "subject", "x": 1}) == [({"by": "author", "x": 1}, 2, False)]
    assert _strips(rows, {"by": "subject", "x": 1}) == [30, 50]


def test_same_version_rows_order_by_id() -> None:
    m = uuid4()
    rows = [
        _edit(m, 3, SUBJECT, {"by": "author"}, row_id=7),
        _edit(m, 3, OTHER, {"by": "x"}, row_id=5),
    ]
    assert _reverts(rows, {"by": "subject"}) == [({"by": "author"}, 3, False)]


def test_other_memories_and_actors_never_produce_a_revert_and_order_is_irrelevant() -> None:
    mine, theirs, untouched = sorted(uuid4() for _ in range(3))
    rows = [
        _edit(mine, 2, AUTHOR, {"a": 1}),
        _edit(mine, 3, SUBJECT, {"a": 2}),
        _edit(theirs, 2, SUBJECT, {"t": 1}),
        _edit(theirs, 3, OTHER, {"t": 2}),
        _edit(untouched, 2, OTHER, {"u": 1}),
        _edit(untouched, 3, AUTHOR, {"u": 2}),
    ]
    live = {
        mine: ({"a": 3}, AUTHOR),
        theirs: ({"t": 3}, AUTHOR),
        untouched: ({"u": 3}, AUTHOR),
    }
    expected = (
        MemoryRevert(
            memory_id=mine,
            reverts=(KeyRevert("a", 2, True, False, 3),),
            from_version=3,
            result={"a": 2},
            prior={},
        ),
    )
    assert plan_details_erasure(rows, live, SUBJECT).reverts == expected
    shuffled = rows[:]
    random.Random(2244).shuffle(shuffled)
    assert plan_details_erasure(shuffled, live, SUBJECT).reverts == expected
    assert plan_details_erasure(rows, live, "nobody") == DetailsErasurePlan(
        memory_ids=(mine, theirs, untouched)
    )


def test_the_next_details_edit_after_the_subjects_holds_their_keys() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, SUBJECT, {"by": "author"}),
        _edit(m, 3, OTHER, {"by": "subject"}),
        _edit(m, 4, AUTHOR, {"by": "other"}),
    ]
    plan = plan_details_erasure(rows, {m: ({"by": "author-2"}, AUTHOR)}, SUBJECT)
    assert plan.strips == (PriorStrip(30, ("by",)),)


def test_a_rollback_the_subject_requested_holds_no_value_of_theirs() -> None:
    """B3: OTHER's session edit was rolled back by the subject; the rollback
    put the author's value back."""
    m = uuid4()
    rows = [
        _edit(m, 2, OTHER, {"by": "author"}, undone_by=1),
        _rollback(m, 3, SUBJECT, {"by": "other"}),
        _edit(m, 4, AUTHOR, {"by": "author"}),
    ]
    assert _strips(rows, {"by": "author-2"}) == []


def test_the_value_an_erasure_row_put_back_belongs_to_the_edit_below_its_block() -> None:
    """Review M2's chain for the strip: OTHER wrote, U wrote over it, U's
    erasure put OTHER's value back, AUTHOR edited over that. OTHER's later
    erasure strips AUTHOR's prior and U's row's prior; a re-run by U's
    pseudonym strips nothing."""
    m = uuid4()
    rows = [
        _edit(m, 2, OTHER, {"by": "creation"}),
        _edit(m, 3, PSEUDONYM, {"by": "other"}),
        _erasure(m, 4, PSEUDONYM, restored_from=3),
        _edit(m, 5, AUTHOR, {"by": "other"}),
    ]
    assert _strips(rows, {"by": "author"}, OTHER) == [30, 50]
    assert _strips(rows, {"by": "author"}, PSEUDONYM) == []


def test_a_prior_over_the_creation_value_is_never_stripped() -> None:
    m = uuid4()
    assert _strips([_edit(m, 2, SUBJECT, {"by": "creation"})], {"by": "subject"}) == []


def test_a_chain_of_erased_actors_is_walked_past_to_the_authors_value() -> None:
    """#2268 (owner decision): S2 wrote over the author's value, S1 over S2's.
    The walk goes past every erased member's value to the last value a
    surviving member wrote, whichever erasure runs first."""
    m = uuid4()
    s1, s2 = "1" * 64, "2" * 64
    rows = [_edit(m, 2, s2, {"by": "author"}), _edit(m, 3, s1, {"by": "s2"})]
    live = {"by": "s1"}
    # S1 first: past S2's value to the author's
    assert _reverts(rows, live, s1, erased_actors=frozenset({s2})) == [({"by": "author"}, 2, False)]
    # S2 first: the live key is S1's, so S2 reverts nothing and strips its
    # value from S1's prior; S1's run still reaches the author's value
    assert _reverts(rows, live, s2, erased_actors=frozenset({s1})) == []
    assert _strips(rows, live, s2, erased_actors=frozenset({s1})) == [30]
    stripped = [rows[0], _edit(m, 3, s1, {})]
    assert _reverts(stripped, live, s1, erased_actors=frozenset({s2})) == [
        ({"by": "author"}, 2, False)
    ]
    # without the set, S2 is a surviving writer and their value comes back
    assert _reverts(rows, live, s1) == [({"by": "s2"}, 3, False)]


def test_an_interleaved_chain_stops_at_the_first_surviving_writer() -> None:
    m = uuid4()
    s1, s2 = "1" * 64, "2" * 64
    rows = [
        _edit(m, 2, s1, {"by": "creation"}),
        _edit(m, 3, OTHER, {"by": "s1"}),
        _edit(m, 4, s2, {"by": "other"}),
        _edit(m, 5, s1, {"by": "s2"}),
    ]
    live = {"by": "s1-again"}
    assert _reverts(rows, live, s1, erased_actors=frozenset({s2})) == [({"by": "other"}, 4, False)]
    assert _reverts(rows, live, s2, erased_actors=frozenset({s1})) == []


def test_a_chain_down_to_an_erased_authors_stripped_prior_is_unavailable() -> None:
    m = uuid4()
    s1, s2 = "1" * 64, "2" * 64
    rows = [_edit(m, 2, s2, erased=True), _edit(m, 3, s1, erased=True)]
    assert _reverts(rows, {"by": "s1"}, s1, author=s2, erased_actors=frozenset({s2})) == [
        (None, 2, True)
    ]
    # the creation value is a surviving author's: it comes back
    kept = [_edit(m, 2, OTHER, {"by": "author"}), _edit(m, 3, s1, {"by": "other"})]
    assert _reverts(kept, {"by": "s1"}, s1, erased_actors=frozenset({s2})) == [
        ({"by": "other"}, 3, False)
    ]


R = 7  # one rollback record id, reused across calls (session_rollback)


def _skipped_then_reverted(m: UUID, restored_from: int | None) -> list[DetailsEdit]:
    """#2266: e1 (the subject's session edit) was skipped because a foreign
    non-details edit sits above it; the session then wrote e2, and the next
    call reverted {e2} alone. The live value is e1's."""
    return [
        _edit(m, 2, SUBJECT, {"by": "author"}, undone_by=R),
        _other_field(m, 3, OTHER),
        _edit(m, 4, SUBJECT, {"by": "subject-1"}, undone_by=R),
        _rollback(m, 5, AUTHOR, {"by": "subject-2"}, restored_from=restored_from),
    ]


def test_a_foreign_row_between_leaves_the_subjects_live_value_theirs() -> None:
    m = uuid4()
    for restored_from in (4, None):  # stamped, and a legacy row (value match)
        rows = _skipped_then_reverted(m, restored_from)
        assert _reverts(rows, {"by": "subject-1"}) == [({"by": "author"}, 2, False)], restored_from
        later = [*rows, _edit(m, 6, OTHER, {"by": "subject-1"})]
        # the rollback's prior (e2's value) and the next edit's prior hold theirs
        assert _strips(later, {"by": "other"}) == [40, 50, 60], restored_from


def test_a_non_details_row_is_never_a_details_write() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 3, SUBJECT, {"by": "author"}),
        _other_field(m, 4, OTHER),
    ]
    assert _reverts(rows, {"by": "subject"}) == [({"by": "author"}, 3, False)]
    assert _reverts(rows, {"by": "subject"}, OTHER) == []
    later = [*rows, _edit(m, 5, AUTHOR, {"by": "subject"})]
    assert _strips(later, {"by": "author"}) == [50]
    assert _strips(later, {"by": "author"}, OTHER) == []


# ---------------------------------------------------------------------------
# #2275: the per-key strip and the restore row, compiled
# ---------------------------------------------------------------------------


def _pg(stmt: Any) -> tuple[str, dict]:
    from sqlalchemy.dialects import postgresql

    compiled = stmt.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split()), compiled.params


async def test_the_editor_strip_locks_once_then_one_update_per_key_set() -> None:
    from sqlalchemy import Select
    from sqlalchemy.sql.dml import Update

    from services.erasure_details import _strip_editor_keys

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[MagicMock(), MagicMock(rowcount=2), MagicMock(rowcount=1)])
    strips = [PriorStrip(9, ("s", "t")), PriorStrip(4, ("s",)), PriorStrip(12, ("s", "t"))]
    assert await _strip_editor_keys(db, strips) == 3

    stmts = [c.args[0] for c in db.execute.await_args_list]
    assert [type(s) for s in stmts] == [Select, Update, Update]
    lock_sql, lock_params = _pg(stmts[0])
    assert lock_sql.endswith("ORDER BY memory_revisions.id FOR UPDATE")
    assert "SKIP LOCKED" not in lock_sql and "NOWAIT" not in lock_sql
    assert sorted(next(v for k, v in lock_params.items() if k.startswith("strip_ids"))) == [
        4,
        9,
        12,
    ]
    groups = []
    for stmt in stmts[1:]:
        sql, params = _pg(stmt)
        assert "jsonb_set(memory_revisions.prior" in sql
        assert "memory_revisions.prior[%(prior_1)s::TEXT] - %(keys_1)s::TEXT[]" in sql
        assert "memory_revisions.prior ? %(prior_2)s" in sql
        assert "jsonb_typeof(memory_revisions.prior[%(prior_1)s::TEXT]) = " in sql
        assert (params["prior_1"], params["prior_2"]) == ("details", "details")
        assert params["jsonb_typeof_1"] == "object"
        assert params["path_1"] == ["details"]
        ids = next(v for k, v in params.items() if k.startswith("ids"))
        groups.append((tuple(params["keys_1"]), sorted(ids)))
    assert groups == [(("s",), [4]), (("s", "t"), [9, 12])]


async def test_an_empty_plan_strips_nothing() -> None:
    from services.erasure_details import _strip_editor_keys

    db = MagicMock()
    db.execute = AsyncMock()
    assert await _strip_editor_keys(db, []) == 0
    db.execute.assert_not_awaited()


async def test_the_authored_arm_has_no_held_ids_arm() -> None:
    from sqlalchemy import and_
    from sqlalchemy.orm import aliased

    from models.memory import MemoryRevision
    from services.erasure_details import strip_predicate

    row = aliased(MemoryRevision)
    sql, params = _pg(and_(*strip_predicate(row, "s")))
    assert "held_ids" not in sql and "ANY" not in sql
    assert "memory_revisions_1.prior ? " in sql
    assert {v for k, v in params.items() if k.startswith("user_id")} == {"s"}


async def test_the_restore_row_holds_the_overwritten_dict_without_the_subjects_keys() -> None:
    """#2275: ``prior`` = ``{"details": <live minus the subject's keys>,
    "restored_from": v}`` — no ``erased`` marker."""
    from types import SimpleNamespace

    from services import erasure_details

    m = uuid4()
    memory = SimpleNamespace(
        id=m,
        type="fact",
        details={"a": 2, "s": "x", "t": "z"},
        version=4,
        updated_at=None,
        deleted_at=None,
        context_id=uuid4(),
        embedding_status="success",
    )
    plan = DetailsErasurePlan(
        reverts=(
            MemoryRevert(
                memory_id=m,
                reverts=(KeyRevert("s", None, False, False, 2),),
                from_version=2,
                result={"a": 2, "t": "z"},
                prior={"a": 2, "t": "z"},
            ),
        ),
        keys_ambiguous=3,
    )
    result = MagicMock()
    result.scalars.return_value = [memory]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    with patch.object(erasure_details, "record_revision") as record:
        report = await erasure_details.restore_editor_details(db, "sub", actor="p" * 64, plan=plan)
    assert memory.details == {"a": 2, "t": "z"} and memory.version == 5
    (call,) = record.call_args_list
    assert call.kwargs["prior"] == {"details": {"a": 2, "t": "z"}, "restored_from": 2}
    assert call.kwargs["writer"] == "erasure" and call.kwargs["actor_user_id"] == "p" * 64
    assert (report.restored, report.unavailable, report.keys_reverted) == (1, 0, 1)
    assert report.keys_ambiguous == 3
    # the written value is a copy: the plan's dicts are not shared with the row
    assert call.kwargs["prior"]["details"] is not plan.reverts[0].prior
    assert memory.details is not plan.reverts[0].result


@pytest.mark.parametrize("unavailable", [False, True])
async def test_a_dry_run_restore_counts_and_writes_nothing(unavailable: bool) -> None:
    from types import SimpleNamespace

    from services import erasure_details

    m = uuid4()
    memory = SimpleNamespace(id=m, type="fact", details={"s": 1}, version=4)
    plan = DetailsErasurePlan(
        reverts=(
            MemoryRevert(
                memory_id=m,
                reverts=(KeyRevert("s", None, False, unavailable, 2),),
                from_version=2,
                result=None,
                prior={},
            ),
        )
    )
    result = MagicMock()
    result.scalars.return_value = [memory]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    with patch.object(erasure_details, "record_revision") as record:
        report = await erasure_details.restore_editor_details(
            db, "sub", actor="p" * 64, dry_run=True, plan=plan
        )
    record.assert_not_called()
    assert memory.details == {"s": 1} and memory.version == 4
    assert (report.restored, report.unavailable) == ((0, 1) if unavailable else (1, 0))
    assert report.to_sync == []


# ---------------------------------------------------------------------------
# #2287: pseudonymize_audit_logs, compiled
# ---------------------------------------------------------------------------


async def _audit_update(emails: list[str], **kwargs: Any) -> tuple[str, dict]:
    from unittest.mock import AsyncMock, MagicMock, patch

    from sqlalchemy.dialects import postgresql

    from services.erasure_details import pseudonymize_audit_logs

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=2))
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        assert await pseudonymize_audit_logs(db, "raw-sub", emails, **kwargs) == 2
    compiled = db.execute.await_args.args[0].compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


def _subs(params: dict) -> set:
    return {v for k, v in params.items() if k.startswith("user_id_")}


async def test_audit_rows_get_the_sub_and_email_pseudonyms_by_the_raw_sub() -> None:
    from utils.hashing import sha256_hex

    sql, params = await _audit_update(["a@example.com"])

    assert sql.count("replace(") == 2
    assert "user_email=CASE WHEN" in sql
    # cut to the column's length: a pseudonym may not fit where the value did
    assert "resource=left(" in sql and 255 in params.values()
    # the WHERE is the raw sub, never a pseudonym (that would hash the hash),
    # through rows locked in id order
    assert "ORDER BY audit_logs_1.id FOR UPDATE" in sql
    assert _subs(params) == {"raw-sub"}
    assert params["user_id"] == sha256_hex("raw-sub", salt="salt")
    assert sha256_hex("a@example.com", salt="salt") in params.values()
    assert "EXISTS" not in sql  # the live erasure: the user row is still there
    # #2345: the subject's request metadata goes too
    assert params["ip_address"] is None and params["user_agent"] is None


async def test_without_an_email_only_the_sub_is_replaced() -> None:
    sql, params = await _audit_update([])

    assert sql.count("replace(") == 1
    assert "user_email" not in sql
    assert _subs(params) == {"raw-sub"}
    assert params["ip_address"] is None and params["user_agent"] is None


async def test_every_email_given_is_replaced_the_longest_first() -> None:
    from utils.hashing import sha256_hex

    sql, params = await _audit_update(["a@x.io", "longer@example.com"])

    assert sql.count("replace(") == 3 and sql.count("WHEN") == 2
    values = list(params.values())
    assert values.index("longer@example.com") < values.index("a@x.io")
    for email in ("a@x.io", "longer@example.com"):
        assert sha256_hex(email, salt="salt") in values


def _exists_subqueries(sql: str) -> list[str]:
    """The ``EXISTS (...)`` bodies of ``sql``, up to their closing paren."""
    import re

    return re.findall(r"EXISTS \((SELECT [^()]*)\)", sql)


async def test_orphaned_only_never_matches_a_user_row() -> None:
    """The ``NOT EXISTS`` reads ``users`` alone, correlated to the audit row
    being matched — a ``FROM users, audit_logs`` cross join would make the
    re-run skip every row as soon as any live user had an audit row."""
    sql, params = await _audit_update([], orphaned_only=True)
    assert params["ip_address"] is None and params["user_agent"] is None

    # in the locking sub-select and in the UPDATE's own WHERE
    assert sql.count("NOT (EXISTS (SELECT") == 2
    assert _exists_subqueries(sql) == [
        "SELECT * \nFROM users \nWHERE users.user_id = audit_logs_1.user_id",
        "SELECT * \nFROM users \nWHERE users.user_id = audit_logs.user_id",
    ]


# ---------------------------------------------------------------------------
# #2302: the restored set of an interrupted run, read back from the log
# ---------------------------------------------------------------------------


def test_memory_scrub_at_parses_the_summary_as_naive_utc() -> None:
    from datetime import datetime

    from services.erasure_details import memory_scrub_at

    summary = {"postgres": {"memory_scrub_at": "2026-10-01T12:00:00Z"}}
    assert memory_scrub_at(summary) == datetime(2026, 10, 1, 12, 0)
    assert memory_scrub_at({"postgres": {"memory_scrub_at": "2026-10-01T21:00:00+09:00"}}) == (
        datetime(2026, 10, 1, 12, 0)
    )
    for empty in (
        None,
        {},
        {"postgres": None},
        {"postgres": {"users": 1}},
        {"note": "x"},
        {"postgres": {"memory_scrub_at": 1}},
    ):
        assert memory_scrub_at(empty) is None
    # a value that is not an ISO time: an error for the strip bound, no bound
    # for the payload re-drive
    malformed = {"postgres": {"memory_scrub_at": "x"}}
    with pytest.raises(ValueError):
        memory_scrub_at(malformed)
    assert memory_scrub_at(malformed, strict=False) is None


async def test_restored_payload_syncs_reads_the_restores_revision_rows() -> None:
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock

    from services.erasure_details import PayloadSync, restored_payload_syncs

    memory_id = uuid4()
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[(memory_id, 4)])))
    since = datetime(2026, 10, 1, 12, 0)

    assert await restored_payload_syncs(db, "p" * 64, since=since) == [PayloadSync(memory_id, 4)]

    stmt = db.execute.await_args.args[0]
    sql = str(stmt)
    params = stmt.compile().params
    assert "max(memory_revisions.version)" in sql
    assert "GROUP BY memory_revisions.memory_id" in sql
    assert "ORDER BY memory_revisions.memory_id" in sql
    assert "memory_revisions.at >=" in sql
    assert params["actor_user_id_1"] == "p" * 64
    assert params["writer_1"] == "erasure"
    assert params["kind_1"] == "updated"
    assert params["at_1"] == since


async def test_restored_payload_syncs_without_a_bound_reads_every_restore() -> None:
    """Review of #2302: no usable scrub time reads every restore row of the
    pseudonym (the patch is idempotent) instead of patching nothing."""
    from unittest.mock import AsyncMock, MagicMock

    from services.erasure_details import PayloadSync, restored_payload_syncs

    memory_id = uuid4()
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[(memory_id, 2)])))
    assert await restored_payload_syncs(db, "p" * 64, since=None) == [PayloadSync(memory_id, 2)]
    stmt = db.execute.await_args.args[0]
    assert "memory_revisions.at" not in str(stmt.whereclause)
    assert stmt.compile().params["actor_user_id_1"] == "p" * 64


# ---------------------------------------------------------------------------
# #2298: pseudonymize_audit_mentions / pseudonymize_audit_trail, compiled
# ---------------------------------------------------------------------------


def _mock_db(rowcount: int = 2) -> Any:
    from unittest.mock import AsyncMock, MagicMock

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=rowcount))
    return db


def _compiled(stmt: Any) -> tuple[str, dict]:
    from sqlalchemy.dialects import postgresql

    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


def _flat(params: dict) -> set:
    """Every bound value, the members of an ``IN`` list included."""
    out: set = set()
    for value in params.values():
        out.update(value if isinstance(value, list) else [value])
    return out


async def _mentions_update(emails: list[str], subs: list[str]) -> tuple[str, dict]:
    from unittest.mock import patch

    from services.erasure_details import pseudonymize_audit_mentions

    db = _mock_db()
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        assert await pseudonymize_audit_mentions(db, emails, subs=subs) == 2
    assert db.execute.await_count == 1
    return _compiled(db.execute.await_args.args[0])


async def test_mentions_match_exact_values_and_never_the_user_id() -> None:
    from utils.hashing import sha256_hex

    sql, params = await _mentions_update(["a@x.io", "longer@example.com"], ["raw-sub"])

    # the UPDATE's own WHERE after the locked ids: the two IN arms, no user_id
    tail = sql.rsplit(" AND ", 1)[1]
    assert tail.startswith("(audit_logs.user_email IN")
    assert "audit_logs.user_email IN" in tail and "audit_logs.resource IN" in tail
    assert "user_id" not in tail
    assert "ORDER BY audit_logs_1.id FOR UPDATE" in sql
    # exact values only: no substring rewrite, no cut (the bound holds)
    assert "replace(" not in sql and "left(" not in sql
    # user_email: one WHEN per email; resource: one per email plus the sub
    assert sql.count("WHEN (audit_logs.user_email =") == 2
    assert sql.count("WHEN (audit_logs.resource =") == 3
    # the subject acted on the user_email rows: their IP / UA go (#2345)
    assert "ip_address=CASE WHEN (audit_logs.user_email IN" in sql
    assert "user_agent=CASE WHEN (audit_logs.user_email IN" in sql
    assert "THEN NULL ELSE audit_logs.ip_address END" in sql
    values = list(params.values())
    assert values.index("longer@example.com") < values.index("a@x.io")
    for email in ("a@x.io", "longer@example.com"):
        assert sha256_hex(email, salt="salt") in values
        assert f"user:{email}" in values
        assert f"user:{sha256_hex(email, salt='salt')}" in values
    assert f"user:{sha256_hex('raw-sub', salt='salt')}" in values
    assert len(f"user:{sha256_hex('raw-sub', salt='salt')}") == 69 <= 255


async def test_mentions_by_sub_alone_leave_user_email() -> None:
    sql, params = await _mentions_update([], ["raw-sub"])

    assert "user_email" not in sql
    assert "ip_address" not in sql and "user_agent" not in sql
    assert "user:raw-sub" in _flat(params)


async def test_mentions_with_nothing_to_match_execute_nothing() -> None:
    from services.erasure_details import count_audit_mentions, pseudonymize_audit_mentions

    db = _mock_db()
    assert await pseudonymize_audit_mentions(db, [], subs=[]) == 0
    assert await count_audit_mentions(db, []) == 0
    db.execute.assert_not_awaited()


async def test_the_count_uses_the_apply_predicate() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from services.erasure_details import count_audit_mentions

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=4)))
    assert await count_audit_mentions(db, ["a@x.io"], subs=["raw-sub"]) == 4
    sql, params = _compiled(db.execute.await_args.args[0])
    assert "FOR UPDATE" not in sql
    assert "audit_logs.user_email IN" in sql and "audit_logs.resource IN" in sql
    assert {"a@x.io", "user:a@x.io", "user:raw-sub"} <= _flat(params)
    assert "user_id" not in sql


async def test_the_trail_locks_once_then_own_rows_then_mentions() -> None:
    from unittest.mock import patch

    from sqlalchemy import Select
    from sqlalchemy.sql.dml import Update

    from services.erasure_details import pseudonymize_audit_trail

    db = _mock_db(3)
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        assert await pseudonymize_audit_trail(db, "raw-sub", ["a@x.io"]) == (3, 3)

    stmts = [c.args[0] for c in db.execute.await_args_list]
    assert [type(s) for s in stmts] == [Select, Update, Update]
    lock_sql, lock_params = _compiled(stmts[0])
    assert lock_sql.endswith("ORDER BY audit_logs_1.id FOR UPDATE")
    # the union: own rows OR the mention arms
    assert "audit_logs_1.user_id = " in lock_sql and " OR " in lock_sql
    assert "audit_logs_1.user_email IN" in lock_sql and "audit_logs_1.resource IN" in lock_sql
    assert {"raw-sub", "a@x.io", "user:a@x.io", "user:raw-sub"} <= _flat(lock_params)
    own_sql, _ = _compiled(stmts[1])
    assert "replace(" in own_sql  # pseudonymize_audit_logs
    mentions_sql, _ = _compiled(stmts[2])
    assert "replace(" not in mentions_sql


async def test_the_trail_without_a_sub_does_only_the_mentions() -> None:
    from unittest.mock import patch

    from sqlalchemy import Select
    from sqlalchemy.sql.dml import Update

    from services.erasure_details import pseudonymize_audit_trail

    db = _mock_db(1)
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        assert await pseudonymize_audit_trail(db, None, ["a@x.io"], pseudonym=PSEUDONYM) == (0, 1)

    stmts = [c.args[0] for c in db.execute.await_args_list]
    assert [type(s) for s in stmts] == [Select, Update]
    lock_sql, lock_params = _compiled(stmts[0])
    # the re-run's pseudonym rows are locked in the same phase
    assert PSEUDONYM in _flat(lock_params)
    assert "raw-sub" not in _flat(lock_params)


async def test_the_trail_with_nothing_to_do_executes_nothing() -> None:
    from services.erasure_details import pseudonymize_audit_trail

    db = _mock_db()
    assert await pseudonymize_audit_trail(db, None, []) == (0, 0)
    db.execute.assert_not_awaited()


async def test_the_dry_run_count_leaves_out_the_own_rows() -> None:
    """The apply rewrites the subject's own rows first: a dry run that also
    counts the own rows names them once, not twice."""
    from unittest.mock import AsyncMock, MagicMock

    from services.erasure_details import count_audit_mentions

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=1)))
    assert await count_audit_mentions(db, ["a@x.io"], exclude_user_id="raw-sub") == 1
    sql, params = _compiled(db.execute.await_args.args[0])
    assert "audit_logs.user_id != " in sql and "raw-sub" in _flat(params)


# ---------------------------------------------------------------------------
# #2345: the re-run's request-metadata sweep, compiled
# ---------------------------------------------------------------------------


async def test_request_metadata_is_cleared_by_the_pseudonym_under_a_lock() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from services.erasure_details import clear_audit_request_metadata

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=iter([4, 7]))))
    assert await clear_audit_request_metadata(db, PSEUDONYM) == {4, 7}
    sql, params = _compiled(db.execute.await_args.args[0])
    assert sql.startswith("UPDATE audit_logs SET ip_address=")
    assert sql.endswith("RETURNING audit_logs.id")
    assert params["ip_address"] is None and params["user_agent"] is None
    assert "ORDER BY audit_logs_1.id FOR UPDATE" in sql
    assert "audit_logs_1.ip_address IS NOT NULL OR audit_logs_1.user_agent IS NOT NULL" in sql
    assert "audit_logs.ip_address IS NOT NULL OR audit_logs.user_agent IS NOT NULL" in sql
    assert {v for k, v in params.items() if k.startswith("user_id_")} == {PSEUDONYM}


async def test_the_request_metadata_dry_run_counts_without_a_lock() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from services.erasure_details import clear_audit_request_metadata

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=iter([2]))))
    assert await clear_audit_request_metadata(db, PSEUDONYM, dry_run=True) == {2}
    sql, params = _compiled(db.execute.await_args.args[0])
    assert sql.startswith("SELECT audit_logs_1.id") and "FOR UPDATE" not in sql
    assert "ip_address IS NOT NULL OR" in sql and PSEUDONYM in _flat(params)


async def test_only_an_initiated_by_that_hashes_to_the_pseudonym_is_rewritten() -> None:
    """The subject's raw sub becomes the pseudonym; an admin's id, a value
    already pseudonymized, a missing key and a non-dict stay."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from sqlalchemy import Select
    from sqlalchemy.sql.dml import Update

    from services.erasure_details import pseudonymize_erasure_audit_initiator
    from utils.hashing import sha256_hex

    pseudonym = sha256_hex("raw-sub", salt="salt")
    rows = [
        (1, {"request_id": "r", "initiated_by": "raw-sub", "is_self_service": False}),
        (2, {"initiated_by": "admin-1"}),
        (3, {"initiated_by": pseudonym}),
        (4, {"request_id": "r"}),
        (5, None),
    ]
    for dry_run in (False, True):
        db = MagicMock()
        db.execute = AsyncMock(
            side_effect=[MagicMock(all=MagicMock(return_value=rows)), MagicMock()]
        )
        with patch("services.account_erasure_service._audit_salt", return_value="salt"):
            assert await pseudonymize_erasure_audit_initiator(db, pseudonym, dry_run=dry_run) == {1}
        stmts = [c.args[0] for c in db.execute.await_args_list]
        select_sql, params = _compiled(stmts[0])
        assert isinstance(stmts[0], Select)
        assert ("FOR UPDATE" in select_sql) is not dry_run
        assert select_sql.split("FOR UPDATE")[0].rstrip().endswith("ORDER BY audit_logs.id")
        assert {"account_erasure", pseudonym} <= _flat(params)
        if dry_run:
            assert len(stmts) == 1
        else:
            (write,) = stmts[1:]
            assert isinstance(write, Update)
            _, write_params = _compiled(write)
            assert write_params["id_1"] == 1
            assert write_params["user_metadata"] == {
                "request_id": "r",
                "initiated_by": pseudonym,
                "is_self_service": False,
            }
