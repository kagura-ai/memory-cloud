"""DB-level pin for the #1049 consolidation gate-switch against the real schema.

Drives ConsolidationPhase.execute() with a real MemoryRepository + real Memory
rows (carrying the #1046 ``reference_count`` column) to verify the adoption gate
end-to-end: an adopted memory promotes; a surfaced-but-unadopted one does not.
Graph + Qdrant are patched (no neural edges, no real delete); LLM is off.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.memory import Memory
from services.sleep.consolidation import ADOPTION_PROMOTE_MIN, ConsolidationPhase
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow


def _working(uid, *, reference_count, access_count, summary):
    return Memory(
        id=uuid4(),
        user_id=uid,
        summary=summary,
        content="content",
        type="note",
        client="test",
        scope="working",
        reference_count=reference_count,
        access_count=access_count,
        importance=0.1,  # below the importance-only promote floor
        created_at=utcnow() - timedelta(days=5),  # young → not on the archival path
    )


@pytest.mark.asyncio
async def test_adoption_gate_switch_against_real_schema(db_session):
    uid = f"user-1049-{uuid4().hex[:8]}"
    adopted = _working(uid, reference_count=ADOPTION_PROMOTE_MIN, access_count=0, summary="adopted")
    surfaced = _working(uid, reference_count=0, access_count=20, summary="surfaced-only")
    db_session.add_all([adopted, surfaced])
    await db_session.commit()

    config = MagicMock()
    config.sleep_llm_provider = ""  # LLM off → deterministic, borderline stays working
    config.sleep_llm_model = "x"

    phase = ConsolidationPhase(db_session, MagicMock())  # real MemoryRepository(db_session)
    with (
        patch("services.sleep.consolidation.GraphService") as GS,
        patch("services.sleep.consolidation.delete_memory_from_qdrant", new_callable=AsyncMock),
    ):
        GS.return_value.stats = AsyncMock(return_value={"total_edges": 0})  # no graph → no neural
        result = await phase.execute(config, uid, None, None, SleepBudget())

    await db_session.refresh(adopted)
    await db_session.refresh(surfaced)

    # Adoption (reference_count >= ADOPTION_PROMOTE_MIN) promotes; high surfacing
    # (access_count=20) with zero adoption does NOT.
    assert adopted.scope == "persistent"
    assert surfaced.scope == "working"
    assert result.details["rule_promoted"] == 1
    assert result.details["rule_deleted"] == 0
