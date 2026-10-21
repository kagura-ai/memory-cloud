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


@pytest.mark.asyncio
async def test_scrub_surviving_memories_pseudonymizes_and_nulls_details() -> None:
    db = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = 4
    db.execute = AsyncMock(return_value=cursor)

    service = AccountErasureService.__new__(AccountErasureService)
    service.db = db

    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        count = await service._scrub_surviving_memories("google-oauth2|erased")

    assert count == 4
    statements = [c.args[0] for c in db.execute.await_args_list]
    sqls = [str(stmt) for stmt in statements]
    # 1) Tombstones hard-deleted (points already gone via delete_user_points;
    #    a pseudonymized tombstone would be unpurgeable by any retention run).
    assert any("DELETE FROM memories" in q and "deleted_at IS NOT NULL" in q for q in sqls)
    # 2) Survivors: user_id pseudonymized + details NULLed in one UPDATE.
    scrub = next(
        stmt
        for stmt, q in zip(statements, sqls, strict=True)
        if "UPDATE memories" in q and "details" in q
    )
    compiled = scrub.compile()
    assert compiled.params["details"] is None
    assert compiled.params["user_id"] != "google-oauth2|erased"
    # 3) The raw sub is cleared from OTHER rows' deleted_by (memories and
    #    contexts) — no raw sub outlives erasure anywhere.
    assert any("UPDATE memories" in q and "deleted_by" in q for q in sqls)
    assert any("UPDATE contexts" in q and "deleted_by" in q for q in sqls)


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

    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
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
    # every event of a memory the subject wrote, and (PR2) the event whose
    # prior holds a details the subject wrote — the next details edit after
    # theirs, found through the subject's own events
    assert "memory_revisions_1.memory_id IN (SELECT memories.id" in strip_sql
    assert "memory_revisions_3.actor_user_id =" in strip_sql
    assert "memory_revisions_3.version < memory_revisions_1.version" in strip_sql
    assert "NOT (EXISTS" in strip_sql
    # #2235: both subqueries correlate to the locked row — no inner FROM
    # list names the bare table (an uncorrelated copy scans every memory's
    # edits) and none correlates to the UPDATE target itself
    assert _from_lists(strip_sql) == [
        "memory_revisions AS memory_revisions_1",
        "memories AS memories_1",
        "memory_revisions AS memory_revisions_2",
        "memories",
        "memory_revisions AS memory_revisions_3",
        "memory_revisions AS memory_revisions_4",
    ]
    assert "memory_revisions_3.memory_id = memory_revisions_1.memory_id" in strip_sql
    assert "memory_revisions_4.memory_id = memory_revisions_1.memory_id" in strip_sql
    assert "memory_revisions_4.version > memory_revisions_3.version" in strip_sql
    assert "= memory_revisions.memory_id" not in strip_sql
    # a details edit an earlier erasure already stripped still counts, both as
    # the subject's edit and as an edit in between
    strip_params = strip_compiled.params
    for alias in ("memory_revisions_3", "memory_revisions_4"):
        assert re.search(rf"\b{alias}\.prior\[%\((\w+)\)s::TEXT\] \?", strip_sql)
    assert sum(1 for v in strip_params.values() if v == "erased") == 2
    assert "memory_revisions_1.prior ? " in strip_sql  # only rows that carry details
    # every actor parameter is the raw sub (the pseudonym comes last)
    assert {
        k: v for k, v in strip_params.items() if k.startswith(("user_id", "actor_user_id"))
    } == {
        "user_id_1": "google-oauth2|erased",
        "actor_user_id_1": "google-oauth2|erased",
        "user_id_2": "google-oauth2|erased",
        "actor_user_id_2": "google-oauth2|erased",
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

    assert await strip_subject_details(db, "pseudo") == StripCounts(4, 1)

    # the memory lock, the strip and the scrub mark — all by the one subject
    assert db.execute.await_count == 3
    for call in db.execute.await_args_list:
        params = call.args[0].compile(dialect=postgresql.dialect()).params
        actors = {k: v for k, v in params.items() if k.startswith(("user_id", "actor_user_id"))}
        assert actors
        assert set(actors.values()) == {"pseudo"}
