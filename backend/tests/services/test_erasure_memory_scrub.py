"""#1336 Gap 2: erasure-time scrub of surviving author memories.

Account erasure deletes the subject's Qdrant points but historically left
their PG memory rows in co-owned/transferred workspaces intact — details
(coordinates included) plus the raw user_id. The scrub pass pseudonymizes
``user_id`` (salted SHA256, the plan_changes/agents convention) and NULLs
``details`` — which also NULLs the generated columns (location_lat/lon,
trigger_from/until) since they derive from details.
"""

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
