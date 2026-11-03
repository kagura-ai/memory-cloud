"""DB-level pin for the #1049 consolidation gate-switch against the real schema.

Drives ConsolidationPhase.execute() with a real MemoryRepository + real Memory
rows (carrying the #1046 ``reference_count`` column) to verify the adoption gate
end-to-end: an adopted memory promotes; a surfaced-but-unadopted one does not.
Graph and the point-delete queue are patched (no neural edges); LLM is off.
Also pins the #2584 archive exemption (``status:open``) on real rows.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from models.memory import Memory
from services.sleep.consolidation import ADOPTION_PROMOTE_MIN, ConsolidationPhase
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow


def _working(uid, *, reference_count, access_count, summary):
    return Memory(
        workspace_id=uuid4(),
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
        patch("services.sleep.consolidation.queue_point_deletes", new_callable=AsyncMock),
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


@pytest.mark.asyncio
async def test_open_memo_survives_archival_until_its_tag_is_removed(db_session):
    """#2584 against the real schema and the real config default: a 40-day-old,
    never-adopted, post-cutoff memo tagged ``status:open`` is not archived; the
    same row is archived on the next run once the tag is gone (no stored state
    besides the tag)."""
    from neural.config import NeuralMemoryConfig

    uid = f"user-2584-{uuid4().hex[:8]}"

    def _old(summary, tags):
        return Memory(
            workspace_id=uuid4(),
            id=uuid4(),
            user_id=uid,
            summary=summary,
            content="content",
            type="tech-debt",
            client="test",
            scope="working",
            reference_count=0,
            access_count=0,
            importance=0.1,
            tags=tags,
            created_at=utcnow() - timedelta(days=40),
        )

    memo = _old("open memo", ["tech-debt", "status:open", "source:review"])
    closed = _old("closed memo", ["tech-debt", "status:done", "source:review"])
    db_session.add_all([memo, closed])
    await db_session.commit()

    config = NeuralMemoryConfig(sleep_llm_provider="")  # LLM off, default exempt tags
    assert config.sleep_archive_exempt_tags == ("status:open",)

    async def _run():
        phase = ConsolidationPhase(db_session, MagicMock())
        with (
            patch("services.sleep.consolidation.GraphService") as GS,
            patch("services.sleep.consolidation.delete_memory_from_qdrant", new_callable=AsyncMock),
            patch(
                "services.sleep.consolidation._adoption_delete_cutoff",
                return_value=utcnow() - timedelta(days=60),
            ),
        ):
            GS.return_value.stats = AsyncMock(return_value={"total_edges": 0})
            return await phase.execute(config, uid, None, None, SleepBudget())

    async def _deleted_by(memory_id):
        return (
            await db_session.execute(select(Memory.deleted_by).where(Memory.id == memory_id))
        ).scalar_one()

    result = await _run()
    await db_session.commit()

    assert result.details["rule_deleted"] == 1
    assert await _deleted_by(closed.id) == "sleep_consolidation"
    assert await _deleted_by(memo.id) is None

    await db_session.execute(
        update(Memory).where(Memory.id == memo.id).values(tags=["tech-debt", "status:done"])
    )
    await db_session.commit()

    result = await _run()
    await db_session.commit()

    assert result.details["rule_deleted"] == 1
    assert await _deleted_by(memo.id) == "sleep_consolidation"
