"""#1336 Gap 2: erasure-time scrub of surviving author memories.

Account erasure deletes the subject's Qdrant points but historically left
their PG memory rows in co-owned/transferred workspaces intact — details
(coordinates included) plus the raw user_id. The scrub pass pseudonymizes
``user_id`` (salted SHA256, the plan_changes/agents convention) and NULLs
``details`` — which also NULLs the generated columns (location_lat/lon,
trigger_from/until) since they derive from details.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.account_erasure_service import AccountErasureService


def _memories_locked_in_id_order(table: str, sql: str) -> str:
    """#2263: the statement over ``table`` reaches its rows only through an
    id-ordered ``FOR UPDATE`` subquery over an alias, and waits on a held row
    (never skips one). Returns the subquery's WHERE clause."""
    flat = " ".join(sql.split())
    match = re.search(
        rf"WHERE {table}\.id IN \(SELECT {table}_1\.id FROM {table} AS {table}_1 "
        rf"WHERE (.+) ORDER BY {table}_1\.id FOR UPDATE\)$",
        flat,
    )
    assert match, sql
    assert "SKIP LOCKED" not in sql
    assert "NOWAIT" not in sql
    return match.group(1)


@pytest.mark.asyncio
async def test_scrub_surviving_memories_pseudonymizes_and_nulls_details() -> None:
    from sqlalchemy.dialects import postgresql

    db = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = 4
    db.execute = AsyncMock(return_value=cursor)

    service = AccountErasureService.__new__(AccountErasureService)
    service.db = db

    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        count = await service._scrub_surviving_memories("google-oauth2|erased")
        pseudonym = service._pseudonym("google-oauth2|erased")

    assert count == 4
    # #2263: the tombstones are hard-deleted earlier (``_delete_subject_tombstones``)
    scrub, memories_deleted_by, contexts_deleted_by = (
        c.args[0].compile(dialect=postgresql.dialect()) for c in db.execute.await_args_list
    )
    # 1) Survivors: user_id pseudonymized + details NULLed in one UPDATE,
    #    over the subject's authored rows (raw sub), locked in id order.
    assert str(scrub).startswith("UPDATE memories SET ")
    assert scrub.params["details"] is None
    assert scrub.params["user_id"] == pseudonym != "google-oauth2|erased"
    assert _memories_locked_in_id_order("memories", str(scrub)) == (
        "memories_1.user_id = %(user_id_1)s::VARCHAR"
    )
    assert scrub.params["user_id_1"] == "google-oauth2|erased"
    # 2) The raw sub is cleared from OTHER rows' deleted_by (memories and
    #    contexts) — no raw sub outlives erasure anywhere. Those rows are
    #    outside the subject's memory locks, so each UPDATE takes its own in
    #    id order (#2263); the parameter is the raw sub, never the pseudonym.
    for table, compiled in (
        ("memories", memories_deleted_by),
        ("contexts", contexts_deleted_by),
    ):
        # (``contexts`` also gets its ``updated_at`` onupdate stamp)
        assert re.match(rf"UPDATE {table} SET (updated_at=now\(\), )?deleted_by=", str(compiled))
        assert _memories_locked_in_id_order(table, str(compiled)) == (
            f"{table}_1.deleted_by = %(deleted_by_1)s::VARCHAR"
        )
        assert compiled.params == {"deleted_by": None, "deleted_by_1": "google-oauth2|erased"}


@pytest.mark.asyncio
async def test_delete_subject_tombstones_locks_in_id_order() -> None:
    """#2263: the subject's tombstones go in one DELETE, scoped to the raw
    sub's tombstoned rows and locked in id order; it returns the rowcount."""
    from sqlalchemy.dialects import postgresql

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=2))
    service = AccountErasureService.__new__(AccountErasureService)
    service.db = db

    assert await service._delete_subject_tombstones("google-oauth2|erased") == 2

    (statement,) = (c.args[0] for c in db.execute.await_args_list)
    compiled = statement.compile(dialect=postgresql.dialect())
    assert str(compiled).startswith("DELETE FROM memories WHERE memories.id IN (SELECT")
    assert _memories_locked_in_id_order("memories", str(compiled)) == (
        "memories_1.user_id = %(user_id_1)s::VARCHAR AND memories_1.deleted_at IS NOT NULL"
    )
    assert compiled.params == {"user_id_1": "google-oauth2|erased"}


@pytest.mark.asyncio
async def test_session_provenance_locks_memories_in_id_order() -> None:
    """#2263: the ``memories.session_id`` scrub matches the subject's own
    (memory, session) pairs and locks the rows in id order."""
    from sqlalchemy.dialects import postgresql

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    service = AccountErasureService.__new__(AccountErasureService)
    service.db = db

    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        counts = await service._erase_session_provenance("google-oauth2|erased")

    assert counts == {"memory_sessions_scrubbed": 1, "session_rollbacks_pseudonymized": 2}
    memories = db.execute.await_args_list[0].args[0]
    assert memories.get_execution_options()["synchronize_session"] is False
    compiled = memories.compile(dialect=postgresql.dialect())
    assert str(compiled).startswith("UPDATE memories SET session_id=")
    assert _memories_locked_in_id_order("memories", str(compiled)) == (
        "(memories_1.id, memories_1.session_id) IN (SELECT memory_revisions.memory_id, "
        "memory_revisions.session_id FROM memory_revisions "
        "WHERE memory_revisions.actor_user_id = %(actor_user_id_1)s::VARCHAR "
        "AND memory_revisions.session_id IS NOT NULL)"
    )
    assert compiled.params == {"session_id": None, "actor_user_id_1": "google-oauth2|erased"}


def test_revision_changed_field_is_the_one_helper() -> None:
    """#2263: ``revision_edited_details`` and the type read share one test —
    the prior still holds the field, or erasure listed it as erased; the
    version rebuild form adds the scrubbed marker."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import aliased

    from models.memory import MemoryRevision
    from services.erasure_details import revision_edited_details
    from services.memory_versions import revision_changed_field

    def render(expr: object) -> tuple[str, dict]:
        compiled = expr.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
        return " ".join(str(compiled).split()), compiled.params

    row = aliased(MemoryRevision)
    assert render(revision_edited_details(row)) == render(revision_changed_field(row, "details"))
    sql, params = render(revision_changed_field(row, "details"))
    assert sql == (
        "(memory_revisions_1.prior ? %(prior_1)s::VARCHAR) OR "
        "(memory_revisions_1.prior[%(prior_2)s::TEXT] ? %(param_1)s::VARCHAR)"
    )
    assert params == {"prior_1": "details", "prior_2": "erased", "param_1": "details"}

    sql, params = render(revision_changed_field(MemoryRevision, "type", scrubbed=True))
    assert sql == (
        "(memory_revisions.prior ? %(prior_1)s::VARCHAR) OR "
        "(memory_revisions.prior[%(prior_2)s::TEXT] ? %(param_1)s::VARCHAR) OR "
        "(memory_revisions.prior[%(prior_3)s::TEXT] ? %(param_2)s::VARCHAR)"
    )
    assert params == {
        "prior_1": "type",
        "prior_2": "erased",
        "param_1": "type",
        "prior_3": "scrubbed",
        "param_2": "type",
    }


_LOCKED_IN_ID_ORDER = re.compile(
    r"WHERE memory_revisions\.id IN \(SELECT memory_revisions_1\.id\s+"
    r"FROM memory_revisions AS memory_revisions_1\s+WHERE .+"
    r"ORDER BY memory_revisions_1\.id FOR UPDATE\)$",
    re.S,
)


def _assert_locked_in_id_order(sql: str) -> None:
    """#2242: the predicate sits in an id-ordered ``FOR UPDATE`` subquery over
    an alias; the erasure waits on a held row (never skips one)."""
    assert _LOCKED_IN_ID_ORDER.search(sql), sql
    assert "SKIP LOCKED" not in sql
    assert "NOWAIT" not in sql


def _from_lists(sql: str) -> list[str]:
    return [" ".join(f.split()) for f in re.findall(r"\bFROM\s+(.+?)\s+WHERE\b", sql, re.S)]


@pytest.mark.asyncio
async def test_erase_memory_revisions_strips_details_and_pseudonymizes_the_actor() -> None:
    """#2043: the revision log of surviving memories keeps no raw sub, no
    session token and no overwritten ``details`` of the subject. #2242: each
    statement is scoped by the subject's rows and locks in id order."""
    from sqlalchemy.dialects import postgresql

    db = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = 3
    db.execute = AsyncMock(return_value=cursor)

    service = AccountErasureService.__new__(AccountErasureService)
    service.db = db

    # the subject's details edit (row 11) and the next one over it (row 12):
    # row 12's prior holds the subject's value (review B3: decided by the
    # provenance walk, read under the memory locks)
    from uuid import uuid4

    from services.erasure_details import DetailsEdit

    memory_id = uuid4()
    edits = [
        DetailsEdit(memory_id, 2, 11, "google-oauth2|erased", "api", {"a": 1}, False),
        DetailsEdit(memory_id, 3, 12, "someone-else", "api", {"b": 2}, False),
    ]
    with (
        patch("services.account_erasure_service._audit_salt", return_value="salt"),
        patch("services.erasure_details._details_edits", new=AsyncMock(return_value=edits)),
    ):
        counts = await service._erase_memory_revisions("google-oauth2|erased")

    # #2244: the strip's rowcount reaches the erasure summary too
    assert counts == {"memory_revisions_pseudonymized": 3, "revision_details_stripped": 3}
    lock, strip, scrub, pseudonymize = (c.args[0] for c in db.execute.await_args_list)
    # #2242 review: the subject's memories are locked first, in id order, so
    # no edit adds to their logs and no rollback / erasure deadlocks on them
    lock_sql = " ".join(str(lock.compile(dialect=postgresql.dialect())).split())
    assert lock_sql.startswith(
        "SELECT memories_1.id FROM memories AS memories_1 WHERE memories_1.id IN (SELECT"
    )
    assert " UNION SELECT " in lock_sql
    assert lock_sql.endswith("ORDER BY memories_1.id FOR UPDATE")
    strip_compiled = strip.compile(dialect=postgresql.dialect())
    strip_sql = str(strip_compiled)
    assert strip_sql.startswith("UPDATE memory_revisions SET prior=((memory_revisions.prior #-")
    _assert_locked_in_id_order(strip_sql)
    # the scope: the memories the subject authored or has a revision on
    assert (
        "WHERE memory_revisions_1.memory_id IN (SELECT memories_1.id "
        "FROM memories AS memories_1 WHERE memories_1.user_id = "
    ) in " ".join(strip_sql.split())
    assert " UNION SELECT memory_revisions_2.memory_id" in strip_sql
    # every event of a memory the subject wrote, and the events whose prior
    # holds a details the subject wrote — the ids the walk found
    assert "memory_revisions_1.memory_id IN (SELECT memories.id" in strip_sql
    assert "memory_revisions_1.id = ANY (" in strip_sql
    assert [v for k, v in strip_compiled.params.items() if k.startswith("held_ids")] == [[12]]
    # no time bound during erasure: every row predates it
    assert ".at <=" not in strip_sql
    assert _from_lists(strip_sql) == [
        "memory_revisions AS memory_revisions_1",
        "memories AS memories_1",
        "memory_revisions AS memory_revisions_2",
        "memories",
    ]
    strip_params = strip_compiled.params
    assert "memory_revisions_1.prior ? " in strip_sql  # only rows that carry details
    # every actor parameter is the raw sub (the pseudonym comes last)
    assert {
        k: v for k, v in strip_params.items() if k.startswith(("user_id", "actor_user_id"))
    } == {
        "user_id_1": "google-oauth2|erased",
        "actor_user_id_1": "google-oauth2|erased",
        "user_id_2": "google-oauth2|erased",
    }
    # PR2: the dropped key is recorded, so a rebuilt version says "unavailable"
    assert ") || " in strip_sql
    assert {"erased": ["details"]} in strip_params.values()

    # PR2: the authored memories' events say their live details was scrubbed
    scrub_compiled = scrub.compile(dialect=postgresql.dialect())
    scrub_sql = str(scrub_compiled)
    assert scrub_sql.startswith("UPDATE memory_revisions SET prior=(memory_revisions.prior || ")
    _assert_locked_in_id_order(scrub_sql)
    assert "memory_revisions_1.memory_id IN (SELECT memories.id" in scrub_sql
    assert "memory_revisions_1.kind =" in scrub_sql
    # a row already marked is not matched again (a re-run by pseudonym, #2243)
    assert "NOT ((memory_revisions_1.prior @> " in scrub_sql
    assert _from_lists(scrub_sql) == ["memory_revisions AS memory_revisions_1", "memories"]
    assert {"scrubbed": ["details"]} in scrub_compiled.params.values()
    assert scrub_compiled.params["user_id_1"] == "google-oauth2|erased"

    compiled = pseudonymize.compile(dialect=postgresql.dialect())
    pseudonymize_sql = str(compiled)
    assert pseudonymize_sql.startswith("UPDATE memory_revisions SET actor_user_id=")
    _assert_locked_in_id_order(pseudonymize_sql)
    assert _from_lists(pseudonymize_sql) == ["memory_revisions AS memory_revisions_1"]
    assert compiled.params["session_id"] is None
    assert compiled.params["actor_user_id"] != "google-oauth2|erased"
    assert compiled.params["actor_user_id_1"] == "google-oauth2|erased"


def test_subject_memory_ids_unions_authored_and_edited() -> None:
    """#2242: the scope is the union — an author may have no revision rows
    (pre-f03 memories, never edited) and an editor did not author."""
    from sqlalchemy.dialects import postgresql

    from services.erasure_details import subject_memory_ids

    compiled = subject_memory_ids("x").compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert " UNION SELECT " in sql
    assert "UNION ALL" not in sql
    assert _from_lists(sql) == ["memories AS memories_1", "memory_revisions AS memory_revisions_1"]
    assert "memories_1.user_id = " in sql
    assert "memory_revisions_1.actor_user_id = " in sql
    assert compiled.params == {"user_id_1": "x", "actor_user_id_1": "x"}


@pytest.mark.asyncio
async def test_pseudonymize_never_takes_the_pseudonym() -> None:
    """#2242: the pseudonymize step matches the raw sub and writes the
    pseudonym the caller derived; a re-run by pseudonym (#2243) runs only
    ``strip_subject_details``."""
    from sqlalchemy.dialects import postgresql

    from services.erasure_details import pseudonymize_revision_actor

    db = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = 2
    db.execute = AsyncMock(return_value=cursor)

    assert await pseudonymize_revision_actor(db, "u", "p" * 64) == 2

    (statement,) = (c.args[0] for c in db.execute.await_args_list)
    compiled = statement.compile(dialect=postgresql.dialect())
    _assert_locked_in_id_order(str(compiled))
    assert compiled.params["actor_user_id_1"] == "u"
    assert compiled.params["actor_user_id"] == "p" * 64
    assert compiled.params["session_id"] is None


@pytest.mark.asyncio
async def test_strip_subject_details_takes_one_subject_string() -> None:
    """#2242: the strip is a function of one subject string — every actor
    parameter of both UPDATEs is it — and returns both rowcounts."""
    from sqlalchemy.dialects import postgresql

    from services.erasure_details import StripCounts, strip_subject_details

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[MagicMock(), MagicMock(rowcount=4), MagicMock(rowcount=1)])

    with patch("services.erasure_details._details_edits", new=AsyncMock(return_value=[])) as read:
        assert await strip_subject_details(db, "pseudo") == StripCounts(4, 1)
    assert read.await_args.args[1] == "pseudo"

    # the memory lock, the strip and the scrub mark — all by the one subject
    assert db.execute.await_count == 3
    for call in db.execute.await_args_list:
        params = call.args[0].compile(dialect=postgresql.dialect()).params
        actors = {k: v for k, v in params.items() if k.startswith(("user_id", "actor_user_id"))}
        assert actors
        assert set(actors.values()) == {"pseudo"}


@pytest.mark.asyncio
async def test_a_re_run_bounds_the_authored_arm_to_rows_the_erasure_saw() -> None:
    """#2243 (review S1): with ``existed_at`` the authored arm of both
    statements reaches only rows written by then; the held-ids arm (a prior
    that holds the subject's value) is not bounded."""
    from datetime import datetime

    from sqlalchemy.dialects import postgresql

    from services.erasure_details import StripCounts, strip_subject_details

    existed_at = datetime(2026, 9, 1, 12, 0, 0)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[MagicMock(), MagicMock(rowcount=2), MagicMock(rowcount=3)])

    with patch("services.erasure_details._details_edits", new=AsyncMock(return_value=[])):
        assert await strip_subject_details(db, "p", existed_at=existed_at) == StripCounts(2, 3)

    _, strip, scrub = (c.args[0] for c in db.execute.await_args_list)
    strip_compiled = strip.compile(dialect=postgresql.dialect())
    strip_sql = " ".join(str(strip_compiled).split())
    assert (
        "(memory_revisions_1.id = ANY (%(held_ids_1)s::BIGINT[]) OR "
        "memory_revisions_1.memory_id IN (SELECT memories.id FROM memories "
        "WHERE memories.user_id = %(user_id_2)s::VARCHAR) AND "
        "memory_revisions_1.at <= %(at_1)s::TIMESTAMP WITHOUT TIME ZONE)"
    ) in strip_sql
    assert strip_compiled.params["at_1"] == existed_at
    scrub_compiled = scrub.compile(dialect=postgresql.dialect())
    assert "memory_revisions_1.at <= " in str(scrub_compiled)
    assert scrub_compiled.params["at_1"] == existed_at


@pytest.mark.asyncio
async def test_a_dry_run_strip_counts_and_writes_nothing() -> None:
    """#2243: the dry run counts with the same predicates; no UPDATE, no lock."""
    from sqlalchemy.dialects import postgresql

    from services.erasure_details import StripCounts, strip_subject_details

    def scalar(n: int) -> MagicMock:
        result = MagicMock()
        result.scalar_one.return_value = n
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[scalar(5), scalar(2)])

    with patch("services.erasure_details._details_edits", new=AsyncMock(return_value=[])):
        assert await strip_subject_details(db, "p", dry_run=True) == StripCounts(5, 2)

    sqls = [
        str(c.args[0].compile(dialect=postgresql.dialect())) for c in db.execute.await_args_list
    ]
    # counts only: no UPDATE, and no row lock (review: a preview blocks no edit)
    assert all(sql.startswith("SELECT count(") for sql in sqls)
    assert not any("FOR UPDATE" in sql for sql in sqls)
