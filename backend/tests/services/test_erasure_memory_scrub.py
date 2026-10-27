"""#1336 Gap 2: erasure-time scrub of surviving author memories.

Account erasure deletes the subject's Qdrant points but historically left
their PG memory rows in co-owned/transferred workspaces intact — details
(coordinates included) plus the raw user_id. The scrub pass pseudonymizes
``user_id`` (salted SHA256, the plan_changes/agents convention) and NULLs
``details`` — which also NULLs the generated columns (location_lat/lon,
trigger_from/until) since they derive from details — except a time memory's
``trigger`` (#2266).
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
    statements = [c.args[0] for c in db.execute.await_args_list]
    # the ORM cannot evaluate the subquery; it must not fetch every id back
    assert all(s.get_execution_options()["synchronize_session"] is False for s in statements)
    scrub, memories_deleted_by, contexts_deleted_by = (
        s.compile(dialect=postgresql.dialect()) for s in statements
    )
    # 1) Survivors: user_id pseudonymized + details NULLed in one UPDATE,
    #    over the subject's authored rows (raw sub), locked in id order.
    assert str(scrub).startswith("UPDATE memories SET ")
    # #2266: a time memory keeps ``{"trigger": ...}`` (its CHECK needs the
    # window); every other row gets NULL
    scrub_sql = " ".join(str(scrub).split())
    assert "details=CAST(CASE WHEN (memories.type = %(type_1)s::VARCHAR" in scrub_sql
    assert "THEN json_build_object(%(json_build_object_1)s::VARCHAR, json_build_object(" in (
        scrub_sql
    )
    assert scrub_sql.count("ELSE NULL END AS JSON)") == 1
    assert scrub.params["type_1"] == "time"
    assert scrub.params["json_build_object_1"] == "trigger"
    # the window keys only, never the trigger's ``text``
    keys = {v for k, v in scrub.params.items() if k.startswith("json_build_object_")}
    assert keys == {"trigger", "year", "month", "day", "hour", "minute", "from", "until"}
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
    assert statement.get_execution_options()["synchronize_session"] is False
    compiled = statement.compile(dialect=postgresql.dialect())
    assert str(compiled).startswith("DELETE FROM memories WHERE memories.id IN (SELECT")
    assert _memories_locked_in_id_order("memories", str(compiled)) == (
        "memories_1.user_id = %(user_id_1)s::VARCHAR AND memories_1.deleted_at IS NOT NULL"
    )
    assert compiled.params == {"user_id_1": "google-oauth2|erased"}


@pytest.mark.asyncio
async def test_dequeue_surviving_embeddings_marks_all_surviving_rows_terminal() -> None:
    """#2363 / #2393: all the subject's surviving rows leave the
    embedding queue in one UPDATE, by the raw sub, locked in id order: status
    ``failed``, the erased-author marker, the retry budget spent and the
    pipeline's clock stamped. Returns the rowcount."""
    from datetime import datetime

    from sqlalchemy.dialects import postgresql

    from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR, MAX_EMBEDDING_RETRIES
    from utils.datetime import utcnow

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=3))
    service = AccountErasureService.__new__(AccountErasureService)
    service.db = db

    before = utcnow()
    assert await service._dequeue_surviving_embeddings("google-oauth2|erased") == 3

    (statement,) = (c.args[0] for c in db.execute.await_args_list)
    assert statement.get_execution_options()["synchronize_session"] is False
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("UPDATE memories SET ")
    assignments = sql.split(" WHERE memories.id IN (SELECT", 1)[0].removeprefix(
        "UPDATE memories SET "
    )
    assert set(assignments.split(", ")) == {
        "embedding_status=%(embedding_status)s::VARCHAR",
        "embedding_error=%(embedding_error)s::VARCHAR",
        "embedding_retry_count=%(embedding_retry_count)s::INTEGER",
        "embedding_attempted_at=%(embedding_attempted_at)s::TIMESTAMP WITHOUT TIME ZONE",
        "embedding_suppressed_at=%(embedding_suppressed_at)s::TIMESTAMP WITH TIME ZONE",
    }
    assert _memories_locked_in_id_order("memories", sql) == (
        "memories_1.user_id = %(user_id_1)s::VARCHAR"
    )
    params = compiled.params
    assert params["embedding_status"] == "failed"
    assert params["embedding_error"] == ERASED_AUTHOR_EMBEDDING_ERROR
    assert ERASED_AUTHOR_EMBEDDING_ERROR.startswith("erased_author: ")
    assert len(ERASED_AUTHOR_EMBEDDING_ERROR) <= 500
    assert params["embedding_retry_count"] == MAX_EMBEDDING_RETRIES
    assert isinstance(params["embedding_attempted_at"], datetime)
    assert params["embedding_attempted_at"].tzinfo is None
    # Suppression is durable policy, independent of the mutable error string.
    assert isinstance(params["embedding_suppressed_at"], datetime)
    assert before <= params["embedding_suppressed_at"] <= utcnow()
    assert params["user_id_1"] == "google-oauth2|erased"
    assert "embedding_status_1" not in params  # successes must also stay terminal


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


@pytest.mark.asyncio
async def test_lock_subject_memories_with_deleted_by_is_one_ordered_statement() -> None:
    """#2263: the live erasure locks the memories the subject deleted in the
    same ordered statement as their own — a second lock phase would let two
    erasures wait on each other's memories. Without the flag (the strip's
    re-lock, a re-run) the scope is unchanged."""
    from sqlalchemy.dialects import postgresql

    from services.erasure_details import lock_subject_memories

    db = MagicMock()
    result = MagicMock()
    result.all.return_value = [("a",), ("b",)]
    db.execute = AsyncMock(return_value=result)
    # #2266: how many rows were locked (the restore logs it)
    assert await lock_subject_memories(db, "raw", deleted_by=True) == 2
    await lock_subject_memories(db, "raw")

    with_deleted_by, plain = (
        c.args[0].compile(dialect=postgresql.dialect()) for c in db.execute.await_args_list
    )
    sql = " ".join(str(with_deleted_by).split())
    assert sql == (
        "SELECT memories_1.id FROM memories AS memories_1 WHERE memories_1.id IN ("
        "SELECT memories_2.id FROM memories AS memories_2 "
        "WHERE memories_2.user_id = %(user_id_1)s::VARCHAR "
        "UNION SELECT memory_revisions_1.memory_id FROM memory_revisions AS memory_revisions_1 "
        "WHERE memory_revisions_1.actor_user_id = %(actor_user_id_1)s::VARCHAR "
        "UNION SELECT memories_3.id FROM memories AS memories_3 "
        "WHERE memories_3.deleted_by = %(deleted_by_1)s::VARCHAR) "
        "ORDER BY memories_1.id FOR UPDATE"
    )
    assert with_deleted_by.params == {
        "user_id_1": "raw",
        "actor_user_id_1": "raw",
        "deleted_by_1": "raw",
    }
    assert "deleted_by" not in str(plain)
    assert plain.params == {"user_id_1": "raw", "actor_user_id_1": "raw"}


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
    # row 12's prior holds the subject's key (#2275: decided per key by the
    # replay, read under the memory locks)
    from services.details_provenance import DetailsErasurePlan, PriorStrip

    plan = DetailsErasurePlan(strips=(PriorStrip(12, ("a",)),))
    with (
        patch("services.account_erasure_service._audit_salt", return_value="salt"),
        patch(
            "services.erasure_details.plan_subject_details", new=AsyncMock(return_value=plan)
        ) as planned,
    ):
        counts = await service._erase_memory_revisions("google-oauth2|erased")
    assert planned.await_args.args[1] == "google-oauth2|erased"

    # #2244: the strip's rowcounts reach the erasure summary too (the per-key
    # editor rows and the authored rows)
    assert counts == {"memory_revisions_pseudonymized": 3, "revision_details_stripped": 6}
    lock, key_lock, keys, strip, scrub, pseudonymize = (
        c.args[0] for c in db.execute.await_args_list
    )
    # #2242 review: the subject's memories are locked first, in id order, so
    # no edit adds to their logs and no rollback / erasure deadlocks on them
    lock_sql = " ".join(str(lock.compile(dialect=postgresql.dialect())).split())
    assert lock_sql.startswith(
        "SELECT memories_1.id FROM memories AS memories_1 WHERE memories_1.id IN (SELECT"
    )
    assert " UNION SELECT " in lock_sql
    assert lock_sql.endswith("ORDER BY memories_1.id FOR UPDATE")
    # #2275: the rows holding the subject's keys are locked in id order, then
    # lose those keys only
    key_lock_compiled = key_lock.compile(dialect=postgresql.dialect())
    assert str(key_lock_compiled).endswith("ORDER BY memory_revisions.id FOR UPDATE")
    assert [v for k, v in key_lock_compiled.params.items() if k.startswith("strip_ids")] == [[12]]
    keys_compiled = keys.compile(dialect=postgresql.dialect())
    assert str(keys_compiled).startswith("UPDATE memory_revisions SET prior=jsonb_set(")
    assert keys_compiled.params["keys_1"] == ["a"]
    assert [v for k, v in keys_compiled.params.items() if k.startswith("ids")] == [[12]]
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
    # the authored arm only: every event of a memory the subject authored
    assert "memory_revisions_1.memory_id IN (SELECT memories.id" in strip_sql
    assert "held_ids" not in strip_sql and "= ANY (" not in strip_sql
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

    with patch("services.erasure_details._updated_rows", new=AsyncMock(return_value=[])) as read:
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
    statements reaches only rows written by then; the per-key editor arm
    (#2275, no rows here) is not bounded."""
    from datetime import datetime

    from sqlalchemy.dialects import postgresql

    from services.erasure_details import StripCounts, strip_subject_details

    existed_at = datetime(2026, 9, 1, 12, 0, 0)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[MagicMock(), MagicMock(rowcount=2), MagicMock(rowcount=3)])

    with patch("services.erasure_details._updated_rows", new=AsyncMock(return_value=[])):
        assert await strip_subject_details(db, "p", existed_at=existed_at) == StripCounts(2, 3)

    _, strip, scrub = (c.args[0] for c in db.execute.await_args_list)
    strip_compiled = strip.compile(dialect=postgresql.dialect())
    strip_sql = " ".join(str(strip_compiled).split())
    assert (
        "memory_revisions_1.memory_id IN (SELECT memories.id FROM memories "
        "WHERE memories.user_id = %(user_id_2)s::VARCHAR) AND "
        "memory_revisions_1.at <= %(at_1)s::TIMESTAMP WITHOUT TIME ZONE"
    ) in strip_sql
    assert "held_ids" not in strip_sql
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

    with patch("services.erasure_details._updated_rows", new=AsyncMock(return_value=[])):
        assert await strip_subject_details(db, "p", dry_run=True) == StripCounts(5, 2)

    sqls = [
        str(c.args[0].compile(dialect=postgresql.dialect())) for c in db.execute.await_args_list
    ]
    # counts only: no UPDATE, and no row lock (review: a preview blocks no edit)
    assert all(sql.startswith("SELECT count(") for sql in sqls)
    assert not any("FOR UPDATE" in sql for sql in sqls)
