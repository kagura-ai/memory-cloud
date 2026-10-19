"""A fact updated twice through remember() recalls as one row (#2123), on Postgres.

The issue's scenario end to end against a real database: three versions of
one fact are written through ``MemoryService.remember()``, the post-embed
detection (``_create_knn_seed_edges``) runs for the two updates with the judge
on (``SUPERSEDE_VERIFY_MODE=llm``), and ``recall()`` is asked for the fact.

Stubbed at the module boundary only: the Qdrant neighbour query
(``db.qdrant.search_memories_qdrant``: each update's top-1 neighbour is the
previous version at cosine 0.80 — a reworded update, below the unverified 0.85
threshold and above the judge-mode 0.75), the judge
(``services.supersede_verify.judge_pair``: a real ``verification_record`` with
real fingerprints, so the read path's staleness rule runs on it) and
``search_service.hybrid_search`` (the candidate pool in the order the
cross-encoder would rank it — oldest wording first — or the hybrid order —
newest first). Everything else — the candidate column writes, the resolver's
liveness / context / fingerprint checks, the fold, the page — runs on the
database.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from models.schemas import RecallRequest, RememberRequest
from neural.config import NeuralMemoryConfig
from services.memory_service import MemoryService, _create_knn_seed_edges
from services.quota_service import DailyReservation
from services.supersede_verify import pair_fingerprint, text_fingerprint, verification_record
from utils.datetime import utcnow

REWORDED_SIMILARITY = 0.80

VERSIONS = (
    "Project Falcon launches on 2026-11-01",
    "Falcon's launch slipped two weeks, to 2026-11-15",
    "Falcon launch moved again — the date is now 2026-12-01",
)


@pytest.fixture
def judge_on(monkeypatch):
    """The judge mode, seed edges off (only the detection is under test)."""
    monkeypatch.setenv("SUPERSEDE_VERIFY_MODE", "llm")
    monkeypatch.setenv("KNN_SEED_ENABLED", "false")
    monkeypatch.delenv("SUPERSEDE_SUGGEST_THRESHOLD_LLM", raising=False)
    monkeypatch.delenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE", raising=False)
    NeuralMemoryConfig.invalidate_cache()
    yield
    NeuralMemoryConfig.invalidate_cache()


@pytest.fixture
def quiet_write():
    """No embedding task, no Redis quota: the parts of remember() not under test."""
    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(
            return_value=DailyReservation(key="quota:test:memories:day", count=1)
        ),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=quota),
    ):
        yield quota


async def _scope(db) -> tuple[str, Workspace, Context]:
    owner = f"fold_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"fold-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"fold-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()
    await db.commit()
    return owner, ws, ctx


async def _remember(db, owner, ws, ctx, summary: str) -> UUID:
    result = await MemoryService(db).remember(
        RememberRequest(summary=summary, content=summary, type="fact", importance=0.6),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    return result.memory_id


async def _write_chain(db, owner, ws, ctx) -> list[UUID]:
    """v1, v2, v3 through remember(), then created_at strictly increasing."""
    ids = [await _remember(db, owner, ws, ctx, summary) for summary in VERSIONS]
    now = utcnow()
    for age_days, memory_id in zip((2, 1, 0), ids, strict=True):
        await db.execute(
            update(Memory)
            .where(Memory.id == memory_id)
            .values(created_at=now - timedelta(days=age_days))
        )
    await db.commit()
    return ids


async def _judge_supersedes(db, *, config, user_id, workspace_id, context_id, new, old):
    """The judge's answer, recorded exactly as ``judge_pair`` records one."""
    return verification_record(
        "supersedes",
        0.95,
        "the same launch date, updated",
        model="stub-judge",
        min_confidence=0.7,
        fingerprint=pair_fingerprint(old, new),
        old_fingerprint=text_fingerprint(old.summary, old.content),
    )


async def _detect(db, new_id: UUID, previous_id: UUID, judge) -> None:
    """The post-embed detection for ``new_id``, its top-1 neighbour ``previous_id``."""
    memory = (await db.execute(select(Memory).where(Memory.id == new_id))).scalar_one()
    neighbours = [
        {"id": str(new_id), "score": 1.0, "payload": {}},
        {"id": str(previous_id), "score": REWORDED_SIMILARITY, "payload": {}},
    ]
    with (
        patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=neighbours)),
        patch("services.supersede_verify.judge_pair", new=judge),
    ):
        await _create_knn_seed_edges(
            db, memory, [0.1] * 8, collection_name="kagura_memories", model_name="stub"
        )


async def _recall(db, owner, ws, ctx, pool: list[UUID], **request_kw):
    svc = MemoryService(db)
    rows = [{"id": str(memory_id), "score": 0.9 - 0.1 * i} for i, memory_id in enumerate(pool)]
    svc.search_service.hybrid_search = AsyncMock(return_value=rows)
    response = await svc.recall(
        RecallRequest(query="When does Falcon launch?", k=5, **request_kw),
        user_id=owner,
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    return response, svc.search_service.hybrid_search


async def _stored_candidate(db, memory_id: UUID):
    row = (await db.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()
    await db.refresh(row, attribute_names=["supersede_candidate"])
    return row.supersede_candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("use_rerank", [True, False])
async def test_three_versions_recall_as_the_newest_with_the_others_folded(
    db_session, judge_on, quiet_write, use_rerank
):
    owner, ws, ctx = await _scope(db_session)
    v1, v2, v3 = await _write_chain(db_session, owner, ws, ctx)
    other = await _remember(db_session, owner, ws, ctx, "Falcon's QA lead is Mina")

    judge = AsyncMock(side_effect=_judge_supersedes)
    await _detect(db_session, v2, v1, judge)
    await _detect(db_session, v3, v2, judge)

    # The write path: both reworded updates are stored, judge-verified.
    assert judge.await_count == 2
    for new_id, old_id in ((v2, v1), (v3, v2)):
        stored = await _stored_candidate(db_session, new_id)
        assert stored["memory_id"] == str(old_id)
        assert stored["similarity"] == REWORDED_SIMILARITY
        assert stored["verification"]["verdict"] == "supersedes"

    # The read path: the cross-encoder ranks the oldest wording first; the
    # hybrid order has the newest first. Same page either way.
    pool = [v1, v2, other, v3] if use_rerank else [v3, other, v2, v1]
    response, hybrid_search = await _recall(db_session, owner, ws, ctx, pool, use_rerank=use_rerank)

    assert hybrid_search.await_args.kwargs["use_rerank"] is use_rerank
    assert [r.memory_id for r in response.results] == [v3, other]
    head = response.results[0]
    assert head.score == pytest.approx(0.9)
    assert head.folded is not None
    assert head.folded.count == 2
    assert head.folded.memory_ids == [v2, v1]
    assert head.supersede_candidate is not None
    assert head.supersede_candidate.memory_id == v2
    assert head.supersede_candidate.verified is True
    assert response.results[1].folded is None
    # #2124: content rides on the head (the newest version's), not a folded one.
    assert head.content == VERSIONS[2]
    assert response.results[1].content == "Falcon's QA lead is Mina"


@pytest.mark.asyncio
async def test_accepting_the_newest_suggestion_keeps_the_oldest_folded(
    db_session, judge_on, quiet_write
):
    """create_edge(v3 → v2) shadows v2 and clears v3's candidate; v1 is
    linked to v2 only and must not come back as a stale row."""
    from models.memory import EDGE_ORIGIN_DECLARED, EDGE_TYPE_SUPERSEDES, NeuralMemoryEdge
    from services.edge_service import accept_supersede_candidate_if_matching

    owner, ws, ctx = await _scope(db_session)
    v1, v2, v3 = await _write_chain(db_session, owner, ws, ctx)
    judge = AsyncMock(side_effect=_judge_supersedes)
    await _detect(db_session, v2, v1, judge)
    await _detect(db_session, v3, v2, judge)
    db_session.add(
        NeuralMemoryEdge(
            user_id=owner,
            src_id=v3,
            dst_id=v2,
            workspace_id=ws.id,
            context_id=ctx.id,
            edge_type=EDGE_TYPE_SUPERSEDES,
            weight=1.0,
            origin=EDGE_ORIGIN_DECLARED,
        )
    )
    await accept_supersede_candidate_if_matching(db_session, src_id=v3, dst_id=v2)
    await db_session.commit()
    assert await _stored_candidate(db_session, v3) is None

    response, _ = await _recall(db_session, owner, ws, ctx, [v1, v2, v3])

    assert [r.memory_id for r in response.results] == [v3]
    assert response.results[0].folded.memory_ids == [v1]
    assert response.results[0].supersede_candidate is None


@pytest.mark.asyncio
async def test_include_superseded_returns_every_version(db_session, judge_on, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    v1, v2, v3 = await _write_chain(db_session, owner, ws, ctx)
    judge = AsyncMock(side_effect=_judge_supersedes)
    await _detect(db_session, v2, v1, judge)
    await _detect(db_session, v3, v2, judge)

    response, _ = await _recall(db_session, owner, ws, ctx, [v1, v2, v3], include_superseded=True)

    assert [r.memory_id for r in response.results] == [v1, v2, v3]
    assert all(r.folded is None for r in response.results)


@pytest.mark.asyncio
async def test_without_a_verdict_a_reworded_update_is_not_stored_nor_folded(
    db_session, judge_on, quiet_write
):
    """The documented limit: no verdict (no provider, the daily cap, a
    failure) below 0.85 → no candidate → nothing to fold."""
    owner, ws, ctx = await _scope(db_session)
    v1, v2, v3 = await _write_chain(db_session, owner, ws, ctx)
    judge = AsyncMock(return_value=None)
    await _detect(db_session, v2, v1, judge)
    await _detect(db_session, v3, v2, judge)

    assert judge.await_count == 2
    assert await _stored_candidate(db_session, v2) is None
    assert await _stored_candidate(db_session, v3) is None

    response, _ = await _recall(db_session, owner, ws, ctx, [v1, v2, v3])

    assert [r.memory_id for r in response.results] == [v1, v2, v3]
    assert all(r.folded is None for r in response.results)
