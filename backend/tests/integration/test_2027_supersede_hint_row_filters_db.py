"""A supersede suggestion honours the recall's row filters (#2027), on Postgres.

A memory under ``docs/`` carries a stored ``supersede_candidate`` naming an
older memory under ``notes/`` in the same context. ``recall(filters=
{"source_uri_prefix": "docs/"})`` returns the row; the suggestion used to name
the ``notes/`` memory and carry its summary anyway. The resolver now holds the
target to the same row filters in its one target query: the row ships, the
suggestion does not. Same for ``type``, ``tags`` (any / all), ``importance``,
``source_type`` and ``scope``; time windows are deliberately not applied.

Stubbed at the module boundary only: ``search_service.hybrid_search`` (the
candidate pool the Qdrant filter would have produced). The memory writes, the
hydration, the resolver's target query, the fold and the page run on the
database.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from models.schemas import RecallRequest, RememberRequest
from services.memory_service import MemoryService
from services.quota_service import DailyReservation
from services.supersede_verify import pair_fingerprint, text_fingerprint, verification_record
from utils.datetime import utcnow

ON = {
    "source_uri": "docs/a",
    "type": "decision",
    "tags": ["infra", "db"],
    "importance": 0.8,
    "source_type": "manual",
    "scope": "persistent",
}


@pytest.fixture(autouse=True)
def default_floor(monkeypatch):
    monkeypatch.delenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE", raising=False)


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
    owner = f"rf_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"rf-ws-{uuid4().hex[:8]}",
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
        name=f"rf-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()
    await db.commit()
    return owner, ws, ctx


async def _write(db, owner, ws, ctx, summary: str, *, age_days: int, **row: Any) -> UUID:
    result = await MemoryService(db).remember(
        RememberRequest(summary=summary, content=summary, type="fact", importance=0.5),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    await db.execute(
        update(Memory)
        .where(Memory.id == result.memory_id)
        .values(created_at=utcnow() - timedelta(days=age_days), **{**ON, **row})
    )
    await db.commit()
    return result.memory_id


async def _load(db, memory_id: UUID) -> Memory:
    row = (await db.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()
    await db.refresh(row)
    return row


async def _suggest(db, new_id: UUID, old_id: UUID, *, judged: bool = False) -> None:
    """Store ``new_id``'s candidate naming ``old_id``, as detection stores one."""
    candidate: dict[str, Any] = {
        "memory_id": str(old_id),
        "similarity": 0.80 if judged else 0.93,
        "detected_at": "2026-10-01T00:00:00Z",
    }
    if judged:
        new, old = await _load(db, new_id), await _load(db, old_id)
        candidate["verification"] = verification_record(
            "supersedes",
            0.95,
            "the same fact, updated",
            model="stub-judge",
            min_confidence=0.7,
            fingerprint=pair_fingerprint(old, new),
            old_fingerprint=text_fingerprint(old.summary, old.content),
        )
    await db.execute(
        update(Memory).where(Memory.id == new_id).values(supersede_candidate=candidate)
    )
    await db.commit()


async def _recall(db, owner, ws, ctx, pool: list[UUID], filters=None):
    svc = MemoryService(db)
    rows = [{"id": str(memory_id), "score": 0.9 - 0.1 * i} for i, memory_id in enumerate(pool)]
    svc.search_service.hybrid_search = AsyncMock(return_value=rows)
    response = await svc.recall(
        RecallRequest(query="the fact", k=5, filters=filters),
        user_id=owner,
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    return {r.memory_id: r for r in response.results}


CASES = [
    pytest.param({"source_uri_prefix": "docs/"}, {"source_uri": "notes/b"}, id="source_uri_prefix"),
    pytest.param({"type": "decision"}, {"type": "fact"}, id="type"),
    pytest.param({"tags": ["infra"]}, {"tags": ["other"]}, id="tags_any"),
    pytest.param(
        {"tags": ["infra", "db"], "tags_match": "all"}, {"tags": ["infra"]}, id="tags_all"
    ),
    pytest.param({"importance": {"gte": 0.7}}, {"importance": 0.3}, id="importance"),
    pytest.param({"source_type": "manual"}, {"source_type": "url"}, id="source_type"),
    pytest.param({"scope": "persistent"}, {"scope": "working"}, id="scope"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("filters", "off"), CASES)
async def test_an_off_filter_target_drops_the_suggestion_not_the_row(
    db_session, quiet_write, filters, off
):
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1, **off)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _suggest(db_session, newer, older)

    filtered = await _recall(db_session, owner, ws, ctx, [newer], filters)
    unfiltered = await _recall(db_session, owner, ws, ctx, [newer])

    assert list(filtered) == [newer]
    assert filtered[newer].supersede_candidate is None
    assert unfiltered[newer].supersede_candidate is not None
    assert unfiltered[newer].supersede_candidate.memory_id == older


@pytest.mark.asyncio
async def test_an_on_filter_target_keeps_the_suggestion(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _suggest(db_session, newer, older)

    page = await _recall(
        db_session,
        owner,
        ws,
        ctx,
        [newer],
        {**{k: v for k, v in ON.items() if k != "source_uri"}, "source_uri_prefix": "docs/"},
    )

    assert page[newer].supersede_candidate is not None
    assert page[newer].supersede_candidate.memory_id == older


@pytest.mark.asyncio
async def test_time_windows_do_not_drop_an_older_target(db_session, quiet_write):
    # Decision B: the target is older than its source by construction.
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=30)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _suggest(db_session, newer, older)
    after = (utcnow() - timedelta(days=7)).isoformat() + "Z"

    page = await _recall(db_session, owner, ws, ctx, [newer], {"created_after": after})

    assert page[newer].supersede_candidate is not None


@pytest.mark.asyncio
async def test_a_widened_tag_filter_keeps_a_target_matched_through_a_spelling(
    db_session, quiet_write
):
    """The resolver gets the widened filter, not ``request.filters``: a target
    tagged ``infra`` survives a ``tags=["Infra"], tags_normalize`` recall
    whose rows matched through that same spelling."""
    owner, ws, ctx = await _scope(db_session)
    older = await _write(db_session, owner, ws, ctx, "the older fact", age_days=1)
    newer = await _write(db_session, owner, ws, ctx, "the newer fact", age_days=0)
    await _suggest(db_session, newer, older)
    expand = AsyncMock(return_value=(["Infra", "infra"], {"Infra": ["infra"]}))

    with patch("services.tag_resolution.expand_tag_filter", new=expand):
        page = await _recall(
            db_session, owner, ws, ctx, [newer], {"tags": ["Infra"], "tags_normalize": True}
        )

    expand.assert_awaited_once()
    assert page[newer].supersede_candidate is not None
    assert page[newer].supersede_candidate.memory_id == older


@pytest.mark.asyncio
async def test_the_folds_reused_resolution_honours_the_filter(db_session, quiet_write):
    """Judge-verified hints, resolved once by the fold and reused by the page
    (#2131): the head whose target is off-filter (and so never in the pool)
    ships without a hint; the other judged row keeps its on-filter one."""
    owner, ws, ctx = await _scope(db_session)
    off_target = await _write(
        db_session, owner, ws, ctx, "the launch date v1", age_days=3, source_uri="notes/b"
    )
    head = await _write(db_session, owner, ws, ctx, "the launch date v2", age_days=0)
    on_target = await _write(db_session, owner, ws, ctx, "the QA lead v1", age_days=2)
    other = await _write(db_session, owner, ws, ctx, "the QA lead v2", age_days=1)
    await _suggest(db_session, head, off_target, judged=True)
    await _suggest(db_session, other, on_target, judged=True)

    svc = MemoryService(db_session)
    resolver = AsyncMock(wraps=svc._resolve_supersede_candidates)
    svc._resolve_supersede_candidates = resolver
    svc.search_service.hybrid_search = AsyncMock(
        return_value=[{"id": str(head), "score": 0.9}, {"id": str(other), "score": 0.8}]
    )
    response = await svc.recall(
        RecallRequest(query="the fact", k=5, filters={"source_uri_prefix": "docs/"}),
        user_id=owner,
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    page = {r.memory_id: r for r in response.results}

    assert list(page) == [head, other]
    assert page[head].supersede_candidate is None
    assert page[other].supersede_candidate is not None
    assert page[other].supersede_candidate.memory_id == on_target
    assert page[other].supersede_candidate.verified is True
    # One resolution, the fold's, reused by the page.
    resolver.assert_awaited_once()
    assert resolver.await_args.kwargs["filters"] == {"source_uri_prefix": "docs/"}
