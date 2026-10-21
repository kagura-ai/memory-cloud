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


@pytest.mark.asyncio
async def test_erase_memory_revisions_strips_details_and_pseudonymizes_the_actor() -> None:
    """#2043: the revision log of surviving memories keeps no raw sub, no
    session token and no overwritten ``details`` of the subject."""
    from sqlalchemy.dialects import postgresql

    db = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = 3
    db.execute = AsyncMock(return_value=cursor)

    service = AccountErasureService.__new__(AccountErasureService)
    service.db = db

    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        count = await service._erase_memory_revisions("google-oauth2|erased")

    assert count == 3
    strip, scrub, pseudonymize = (c.args[0] for c in db.execute.await_args_list)
    strip_sql = str(strip.compile(dialect=postgresql.dialect()))
    assert strip_sql.startswith("UPDATE memory_revisions SET prior=((memory_revisions.prior #-")
    # every event of a memory the subject wrote, and (PR2) the event whose
    # prior holds a details the subject wrote — the next details edit after
    # theirs, found through the subject's own events
    assert "memory_revisions.memory_id IN (SELECT memories.id" in strip_sql
    assert "memory_revisions_1.actor_user_id =" in strip_sql
    assert "memory_revisions_1.version < memory_revisions.version" in strip_sql
    assert "NOT (EXISTS" in strip_sql
    # #2235: both subqueries correlate to the UPDATE target — no FROM list
    # names the bare table (an uncorrelated copy scans every memory's edits)
    from_lists = re.findall(r"\bFROM ([^\n]*?)\s*(?:\n|$)", strip_sql)
    assert from_lists == [
        "memories",
        "memory_revisions AS memory_revisions_1",
        "memory_revisions AS memory_revisions_2",
    ]
    assert "memory_revisions_2.memory_id = memory_revisions.memory_id" in strip_sql
    assert "memory_revisions_2.version > memory_revisions_1.version" in strip_sql
    assert "memory_revisions.prior ? " in strip_sql  # only rows that carry details
    # PR2: the dropped key is recorded, so a rebuilt version says "unavailable"
    assert ") || " in strip_sql
    assert {"erased": ["details"]} in strip.compile(dialect=postgresql.dialect()).params.values()

    # PR2: the authored memories' events say their live details was scrubbed
    scrub_compiled = scrub.compile(dialect=postgresql.dialect())
    assert "memory_revisions.memory_id IN (SELECT memories.id" in str(scrub_compiled)
    assert "memory_revisions.kind =" in str(scrub_compiled)
    assert {"scrubbed": ["details"]} in scrub_compiled.params.values()

    compiled = pseudonymize.compile(dialect=postgresql.dialect())
    assert str(compiled).startswith("UPDATE memory_revisions SET actor_user_id=")
    assert compiled.params["session_id"] is None
    assert compiled.params["actor_user_id"] != "google-oauth2|erased"
    assert compiled.params["actor_user_id_1"] == "google-oauth2|erased"
