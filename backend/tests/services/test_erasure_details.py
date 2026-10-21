"""#2244: ``restore_targets`` — the value an erased editor's details go back to.

Pure: rows are ``DetailsEdit``s (the details edits of the candidate memories,
in any order); the target is the prior of the lowest row of the subject's
trailing run of details edits, NULL when an earlier erasure removed it.
"""

from __future__ import annotations

import random
from uuid import UUID, uuid4

from services.erasure_details import (
    DetailsEdit,
    RestoreTarget,
    held_details_ids,
    restore_targets,
)

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
) -> DetailsEdit:
    return DetailsEdit(
        memory_id=memory_id,
        version=version,
        id=row_id if row_id is not None else version * 10,
        actor_user_id=actor,
        writer=writer,
        details=None if erased else details,
        erased=erased,
        undone_by=undone_by,
        restored_from=restored_from,
    )


def _erasure(memory_id: UUID, version: int, actor: str, restored_from: int) -> DetailsEdit:
    """The row ``restore_editor_details`` writes."""
    return _edit(
        memory_id, version, actor, erased=True, writer="erasure", restored_from=restored_from
    )


def _rollback(memory_id: UUID, version: int, actor: str, undid: object) -> DetailsEdit:
    """A session rollback's put-back row: its prior is the value it undid."""
    return _edit(memory_id, version, actor, undid, writer="rollback")


def test_latest_edit_by_the_subject_goes_back_to_the_previous_writers_value() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 4, SUBJECT, {"by": "author"}),
    ]
    assert restore_targets(rows, SUBJECT) == [
        RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=4)
    ]


def test_the_subjects_consecutive_edits_are_walked_past() -> None:
    """Versions 3, 5, 7 are the subject's details edits (non-details edits in
    between are not details edits, so not in ``rows``): the value goes back
    to the prior of version 3, the author's — not the subject's first value."""
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 3, SUBJECT, {"by": "author"}),
        _edit(m, 5, SUBJECT, {"by": "subject-1"}),
        _edit(m, 7, SUBJECT, {"by": "subject-2"}),
    ]
    assert restore_targets(rows, SUBJECT) == [
        RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=3)
    ]


def test_every_details_edit_by_the_subject_goes_back_to_the_creation_value() -> None:
    m = uuid4()
    rows = [_edit(m, 2, SUBJECT, {"at": "creation"}), _edit(m, 3, SUBJECT, {"by": "subject"})]
    assert restore_targets(rows, SUBJECT) == [
        RestoreTarget(memory_id=m, details={"at": "creation"}, unavailable=False, from_version=2)
    ]


def test_the_creation_value_may_be_null() -> None:
    m = uuid4()
    (target,) = restore_targets([_edit(m, 2, SUBJECT, None)], SUBJECT)
    assert target.details is None
    assert target.unavailable is False


def test_a_later_edit_by_someone_else_means_no_restore() -> None:
    m = uuid4()
    rows = [_edit(m, 2, SUBJECT, {"by": "author"}), _edit(m, 3, OTHER, {"by": "subject"})]
    assert restore_targets(rows, SUBJECT) == []


def test_an_erased_prior_gives_null_and_unavailable() -> None:
    """Double erasure: the subject's run sits on a value an earlier erasure
    removed (its writer was erased first). The walk does not go further."""
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 3, PSEUDONYM, {"by": "author"}),
        _edit(m, 4, SUBJECT, erased=True),
        _edit(m, 5, SUBJECT, {"by": "subject"}),
    ]
    assert restore_targets(rows, SUBJECT) == [
        RestoreTarget(memory_id=m, details=None, unavailable=True, from_version=4)
    ]


def test_an_erasure_row_on_top_is_not_the_subjects_writing() -> None:
    """Idempotency of a re-run by pseudonym (#2243): the erasure row carries
    the pseudonym as actor and reverted the run from ``restored_from`` up;
    the value it put back was written below that run. The same row written
    as ``api`` would be the subject's edit — that is why the writer value
    exists."""
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 3, PSEUDONYM, {"by": "author"}),
        _erasure(m, 4, PSEUDONYM, restored_from=3),
    ]
    assert restore_targets(rows, PSEUDONYM) == []
    as_api = [*rows[:2], _edit(m, 4, PSEUDONYM, erased=True, writer="api")]
    assert restore_targets(as_api, PSEUDONYM) == [
        RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=3)
    ]


def test_an_erasure_row_below_ends_the_run() -> None:
    """An earlier erasure put back the author's value; the subject's edit on
    top of it goes back to that value."""
    m = uuid4()
    rows = [
        _edit(m, 2, PSEUDONYM, {"by": "author"}),
        _erasure(m, 3, PSEUDONYM, restored_from=2),
        _edit(m, 4, SUBJECT, {"by": "author"}),
    ]
    assert restore_targets(rows, SUBJECT) == [
        RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=4)
    ]


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
    assert restore_targets(rows, "member-v") == [
        RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=2)
    ]
    after = [_edit(m, 2, p_v, {"by": "author"}), *rows[1:], _erasure(m, 5, p_v, restored_from=2)]
    assert restore_targets(after, p_v) == []
    assert restore_targets(after, p_u) == []


def test_the_subjects_rollback_of_another_session_is_not_their_writing() -> None:
    """Review M1: T's session edits A's memory, the owner U rolls it back.
    The live value is A's again; nothing of U's is on the memory, and the
    rolled-back value must not come back."""
    m = uuid4()
    rows = [
        _edit(m, 2, OTHER, {"by": "author"}, undone_by=7),
        _rollback(m, 3, SUBJECT, {"by": "other"}),
    ]
    assert restore_targets(rows, SUBJECT) == []


def test_a_rollback_over_the_subjects_value_leaves_the_subject_as_the_writer() -> None:
    """Review M1 variants: U edits (dU over dA), T's session edits over it and
    is rolled back — by U or by the author. dU is live and U's: dA comes back,
    not T's rolled-back value."""
    m = uuid4()
    for requester in (SUBJECT, AUTHOR):
        rows = [
            _edit(m, 2, SUBJECT, {"by": "author"}),
            _edit(m, 3, OTHER, {"by": "subject"}, undone_by=7),
            _edit(m, 4, OTHER, {"by": "other-1"}, undone_by=7),
            _rollback(m, 5, requester, {"by": "other-2"}),
        ]
        assert restore_targets(rows, SUBJECT) == [
            RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=2)
        ], requester


def test_a_rollback_block_ends_at_another_rollbacks_mark() -> None:
    """Events a rollback skipped carry another rollback's id: they are not in
    the block the put-back row reverted."""
    m = uuid4()
    rows = [
        _edit(m, 2, SUBJECT, {"by": "author"}, undone_by=5),
        _edit(m, 3, OTHER, {"by": "subject"}, undone_by=7),
        _rollback(m, 4, AUTHOR, {"by": "other"}),
    ]
    (target,) = restore_targets(rows, SUBJECT)
    assert (target.details, target.from_version) == ({"by": "author"}, 2)


def test_a_rollback_with_nothing_marked_below_drops_only_itself() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, SUBJECT, {"by": "author"}),
        _rollback(m, 3, AUTHOR, {"by": "subject"}),
    ]
    (target,) = restore_targets(rows, SUBJECT)
    assert target.from_version == 2


def test_same_version_rows_order_by_id() -> None:
    m = uuid4()
    rows = [
        _edit(m, 3, SUBJECT, {"by": "author"}, row_id=7),
        _edit(m, 3, OTHER, {"by": "x"}, row_id=5),
    ]
    (target,) = restore_targets(rows, SUBJECT)
    assert target.details == {"by": "author"}


def test_other_memories_and_actors_never_produce_a_target_and_order_is_irrelevant() -> None:
    mine, theirs, untouched = sorted(uuid4() for _ in range(3))
    rows = [
        _edit(mine, 2, AUTHOR, {"a": 1}),
        _edit(mine, 3, SUBJECT, {"a": 2}),
        _edit(theirs, 2, SUBJECT, {"t": 1}),
        _edit(theirs, 3, OTHER, {"t": 2}),
        _edit(untouched, 2, OTHER, {"u": 1}),
        _edit(untouched, 3, AUTHOR, {"u": 2}),
    ]
    expected = [RestoreTarget(memory_id=mine, details={"a": 2}, unavailable=False, from_version=3)]
    assert restore_targets(rows, SUBJECT) == expected
    shuffled = rows[:]
    random.Random(2244).shuffle(shuffled)
    assert restore_targets(shuffled, SUBJECT) == expected
    assert restore_targets(rows, "nobody") == []


def test_targets_come_in_memory_id_order() -> None:
    ids = [uuid4() for _ in range(5)]
    rows = [_edit(m, 2, SUBJECT, {"m": str(m)}) for m in ids]
    assert [t.memory_id for t in restore_targets(rows, SUBJECT)] == sorted(ids)


# ---------------------------------------------------------------------------
# #2243 (review B3): which priors hold a value the subject wrote
# ---------------------------------------------------------------------------


def test_the_next_details_edit_after_the_subjects_holds_their_value() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, SUBJECT, {"by": "author"}),
        _edit(m, 3, OTHER, {"by": "subject"}),
        _edit(m, 4, AUTHOR, {"by": "other"}),
    ]
    assert held_details_ids(rows, SUBJECT) == [30]


def test_a_rollback_the_subject_requested_holds_no_value_of_theirs() -> None:
    """B3: OTHER's session edit was rolled back by the subject; the rollback
    put the author's value back, so the next edit's prior is the author's —
    and the rollback's own prior is OTHER's undone value."""
    m = uuid4()
    rows = [
        _edit(m, 2, OTHER, {"by": "author"}, undone_by=1),
        _rollback(m, 3, SUBJECT, {"by": "other"}),
        _edit(m, 4, AUTHOR, {"by": "author"}),
    ]
    assert held_details_ids(rows, SUBJECT) == []


def test_a_rollback_over_the_subjects_value_leaves_their_value_held() -> None:
    """The subject wrote, OTHER's session wrote over it, the author rolled
    the session back: the subject's value is live again, so both the undone
    edit's prior and the next edit's prior hold it."""
    m = uuid4()
    rows = [
        _edit(m, 2, SUBJECT, {"by": "creation"}),
        _edit(m, 3, OTHER, {"by": "subject"}, undone_by=1),
        _rollback(m, 4, AUTHOR, {"by": "other"}),
        _edit(m, 5, AUTHOR, {"by": "subject"}),
    ]
    assert held_details_ids(rows, SUBJECT) == [30, 50]


def test_the_value_an_erasure_row_put_back_belongs_to_the_edit_below_its_block() -> None:
    """Review M2's chain for the strip: OTHER wrote, U wrote over it, U's
    erasure put OTHER's value back, AUTHOR edited over that. OTHER's later
    erasure must strip AUTHOR's prior (OTHER's value) — and U's row, whose
    prior is OTHER's value too. A re-run by U's pseudonym holds nothing."""
    m = uuid4()
    rows = [
        _edit(m, 2, OTHER, {"by": "creation"}),
        _edit(m, 3, PSEUDONYM, {"by": "other"}),
        _erasure(m, 4, PSEUDONYM, restored_from=3),
        _edit(m, 5, AUTHOR, {"by": "other"}),
    ]
    assert held_details_ids(rows, OTHER) == [30, 50]
    assert held_details_ids(rows, PSEUDONYM) == []


def test_a_prior_over_the_creation_value_is_never_held() -> None:
    m = uuid4()
    rows = [_edit(m, 2, SUBJECT, {"by": "creation"})]
    assert held_details_ids(rows, SUBJECT) == []


def test_held_ids_ignore_input_order_and_other_memories() -> None:
    a, b = uuid4(), uuid4()
    rows = [
        _edit(a, 2, SUBJECT, {"by": "creation"}, row_id=5),
        _edit(a, 3, OTHER, {"by": "subject"}, row_id=9),
        _edit(b, 2, OTHER, {"by": "creation"}, row_id=6),
        _edit(b, 3, AUTHOR, {"by": "other"}, row_id=7),
    ]
    for _ in range(5):
        random.shuffle(rows)
        assert held_details_ids(rows, SUBJECT) == [9]
