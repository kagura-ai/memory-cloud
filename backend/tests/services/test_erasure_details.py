"""#2244: ``restore_targets`` — the value an erased editor's details go back to.

Pure: rows are ``DetailsEdit``s (the details edits of the candidate memories,
in any order); the target is the prior of the lowest row of the subject's
trailing run of details edits, NULL when an earlier erasure removed it.
"""

from __future__ import annotations

import random
from uuid import UUID, uuid4

from services.erasure_details import DetailsEdit, RestoreTarget, restore_targets

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
) -> DetailsEdit:
    return DetailsEdit(
        memory_id=memory_id,
        version=version,
        id=row_id if row_id is not None else version * 10,
        actor_user_id=actor,
        writer=writer,
        details=None if erased else details,
        erased=erased,
    )


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
    the pseudonym as actor; with ``writer='erasure'`` it ends the run. The
    same row written as ``api`` would be the subject's edit — that is why
    the writer value exists."""
    m = uuid4()
    rows = [
        _edit(m, 2, PSEUDONYM, {"by": "author"}),
        _edit(m, 3, PSEUDONYM, erased=True, writer="erasure"),
    ]
    assert restore_targets(rows, PSEUDONYM) == []
    as_api = [rows[0], _edit(m, 3, PSEUDONYM, erased=True, writer="api")]
    assert restore_targets(as_api, PSEUDONYM) == [
        RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=2)
    ]


def test_an_erasure_row_below_ends_the_run() -> None:
    """An earlier erasure restored another member's value; the subject's edit
    on top of it goes back to that value."""
    m = uuid4()
    rows = [
        _edit(m, 2, PSEUDONYM, {"by": "author"}),
        _edit(m, 3, PSEUDONYM, erased=True, writer="erasure"),
        _edit(m, 4, SUBJECT, {"by": "author"}),
    ]
    assert restore_targets(rows, SUBJECT) == [
        RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=4)
    ]


def test_a_rollback_the_subject_requested_counts_as_their_write() -> None:
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 3, SUBJECT, {"by": "author"}),
        _edit(m, 4, SUBJECT, {"by": "subject"}, writer="rollback"),
    ]
    (target,) = restore_targets(rows, SUBJECT)
    assert target.details == {"by": "author"}
    assert target.from_version == 3


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
