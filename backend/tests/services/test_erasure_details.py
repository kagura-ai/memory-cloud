"""#2244: ``restore_targets`` — the value an erased editor's details go back to.

Pure: rows are ``DetailsEdit``s (the details edits of the candidate memories,
in any order); the target is the prior of the lowest row of the subject's
trailing run of details edits, NULL when an earlier erasure removed it.
"""

from __future__ import annotations

import random
from typing import Any
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
    """An ``updated`` row that changed no ``details`` (#2266: in the walk)."""
    return _edit(memory_id, version, actor, details_changed=False)


def _erasure(memory_id: UUID, version: int, actor: str, restored_from: int) -> DetailsEdit:
    """The row ``restore_editor_details`` writes."""
    return _edit(
        memory_id, version, actor, erased=True, writer="erasure", restored_from=restored_from
    )


def _rollback(
    memory_id: UUID, version: int, actor: str, undid: object, restored_from: int | None = None
) -> DetailsEdit:
    """A session rollback's put-back row: its prior is the value it undid
    (and, since #2266, the lowest version of the block it reverted)."""
    return _edit(memory_id, version, actor, undid, writer="rollback", restored_from=restored_from)


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


def test_a_chain_of_erased_actors_is_walked_past_to_the_authors_value() -> None:
    """#2268 (owner decision): S2 wrote over the author's value, S1 over S2's;
    both erased before the #2235 fix, so S1's own row still holds S2's value
    in its prior. The walk goes past every erased member's edit to the last
    value a surviving member wrote, whichever erasure is processed first."""
    m = uuid4()
    s1, s2 = "1" * 64, "2" * 64
    author = {"by": "author"}
    rows = [_edit(m, 2, s2, author), _edit(m, 3, s1, {"by": "s2"})]
    # S1 first: past S2's edit to the author's value
    assert restore_targets(rows, s1, erased_actors={s2}) == [
        RestoreTarget(memory_id=m, details=author, unavailable=False, from_version=2)
    ]
    # S2 first: S1's edit is the newest, so S2 restores nothing; S2's strip
    # then removes S2's value from S1's prior; S1's run still reaches the author
    assert restore_targets(rows, s2, erased_actors={s1}) == []
    stripped = [rows[0], _edit(m, 3, s1, erased=True)]
    assert restore_targets(stripped, s1, erased_actors={s2}) == [
        RestoreTarget(memory_id=m, details=author, unavailable=False, from_version=2)
    ]
    # after either run: the erasure row names the whole run, so the other
    # erasure finds no edit of its subject as the newest
    after = [*rows, _erasure(m, 4, s1, restored_from=2)]
    assert restore_targets(after, s2, erased_actors={s1}) == []
    assert restore_targets(after, s1, erased_actors={s2}) == []
    # without the set, S2 is a surviving writer and their value would come back
    assert restore_targets(rows, s1)[0].details == {"by": "s2"}


def test_an_interleaved_chain_stops_at_the_first_surviving_writer() -> None:
    """S1 and S2 interleave with OTHER (a surviving member): the walk stops
    at OTHER's edit, so OTHER's value comes back, not the creation value."""
    m = uuid4()
    s1, s2 = "1" * 64, "2" * 64
    rows = [
        _edit(m, 2, s1, {"by": "creation"}),
        _edit(m, 3, OTHER, {"by": "s1"}),
        _edit(m, 4, s2, {"by": "other"}),
        _edit(m, 5, s1, {"by": "s2"}),
    ]
    for subject, others in ((s1, {s2}), (s2, {s1})):
        targets = restore_targets(rows, subject, erased_actors=others)
        if subject == s1:
            assert targets == [
                RestoreTarget(
                    memory_id=m, details={"by": "other"}, unavailable=False, from_version=4
                )
            ]
        else:
            assert targets == []  # S1's edit is the newest


def test_a_chain_down_to_an_erased_authors_stripped_prior_is_unavailable() -> None:
    """The author was erased too: their erasure stripped every prior of the
    memory, so below the whole run there is no value to put back."""
    m = uuid4()
    s1, s2 = "1" * 64, "2" * 64
    rows = [_edit(m, 2, s2, erased=True), _edit(m, 3, s1, erased=True)]
    assert restore_targets(rows, s1, erased_actors={s2}) == [
        RestoreTarget(memory_id=m, details=None, unavailable=True, from_version=2)
    ]
    # the creation value is a surviving author's: it comes back
    kept = [_edit(m, 2, OTHER, {"by": "author"}), _edit(m, 3, s1, {"by": "other"})]
    assert restore_targets(kept, s1, erased_actors={s2})[0].details == {"by": "other"}
    assert restore_targets(kept[1:], s1, erased_actors={s2})[0].details == {"by": "other"}


# ---------------------------------------------------------------------------
# #2266: rollback blocks are contiguous in the whole log, not in details edits
# ---------------------------------------------------------------------------

R = 7  # one rollback record id, reused across calls (session_rollback)


def _skipped_then_reverted(m: UUID, restored_from: int | None) -> list[DetailsEdit]:
    """e1 (the subject's session edit) was skipped ``modified_since`` because
    a foreign non-details edit f sits above it, and marked with R; the
    session then wrote e2, and the next call reverted {e2} alone under the
    same record R. The live value is e1's."""
    return [
        _edit(m, 2, SUBJECT, {"by": "author"}, undone_by=R),
        _other_field(m, 3, OTHER),
        _edit(m, 4, SUBJECT, {"by": "subject-1"}, undone_by=R),
        _rollback(m, 5, AUTHOR, {"by": "subject-2"}, restored_from=restored_from),
    ]


def test_a_foreign_row_between_ends_the_rollback_block() -> None:
    m = uuid4()
    for restored_from in (4, None):  # stamped, and a legacy row (mark walk)
        assert restore_targets(_skipped_then_reverted(m, restored_from), SUBJECT) == [
            RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=2)
        ], restored_from


def test_a_foreign_row_between_keeps_the_subjects_live_value_held() -> None:
    """A later edit over the value e1 wrote holds it; so does the rollback's
    prior (e2's value, the subject's)."""
    m = uuid4()
    for restored_from in (4, None):
        rows = [*_skipped_then_reverted(m, restored_from), _edit(m, 6, OTHER, {"by": "subject-1"})]
        assert held_details_ids(rows, SUBJECT) == [40, 50, 60], restored_from


def test_a_stamped_rollback_drops_only_its_block() -> None:
    """e1 was skipped ``not_permitted`` under R and e2 reverted later under R,
    with nothing between: the marks are contiguous, but only e2 was in the
    block. ``restored_from`` says so; a legacy row cannot tell them apart."""
    m = uuid4()
    rows = [
        _edit(m, 2, SUBJECT, {"by": "author"}, undone_by=R),
        _edit(m, 3, SUBJECT, {"by": "subject-1"}, undone_by=R),
        _rollback(m, 4, AUTHOR, {"by": "subject-2"}, restored_from=3),
    ]
    assert restore_targets(rows, SUBJECT) == [
        RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=2)
    ]
    assert held_details_ids([*rows, _edit(m, 5, OTHER, {"by": "subject-1"})], SUBJECT) == [
        30,
        40,
        50,
    ]
    legacy = [*rows[:2], _rollback(m, 4, AUTHOR, {"by": "subject-2"})]
    assert restore_targets(legacy, SUBJECT) == []


def test_a_non_details_row_is_never_an_authored_details_edit() -> None:
    """OTHER's tag edit above the subject's details edit leaves the subject
    the writer of the live details; it is never the value's writer below."""
    m = uuid4()
    rows = [
        _edit(m, 2, AUTHOR, {"by": "creation"}),
        _edit(m, 3, SUBJECT, {"by": "author"}),
        _other_field(m, 4, OTHER),
    ]
    assert restore_targets(rows, SUBJECT) == [
        RestoreTarget(memory_id=m, details={"by": "author"}, unavailable=False, from_version=3)
    ]
    assert restore_targets(rows, OTHER) == []
    # the next details edit's prior is the subject's value, past OTHER's row
    assert held_details_ids([*rows, _edit(m, 5, AUTHOR, {"by": "subject"})], SUBJECT) == [50]
    assert held_details_ids([*rows, _edit(m, 5, AUTHOR, {"by": "subject"})], OTHER) == []


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
