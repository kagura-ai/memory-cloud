"""The user facts lane of recall (#3014), on Postgres.

Memories are written through ``MemoryService.remember()`` and read back by
``MemoryService.recall()``. Stubbed at the module boundary only: the embedding
task, the Redis quota and ``SearchService.hybrid_search`` (each query's
candidate pool in a fixed order; the vector store's own type, context and
author filters are not under test here). Hydration, the context and trust-tier
filters, supersede shadowing, the rows and the lane's session run on the
database.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import (
    EDGE_ORIGIN_DECLARED,
    EDGE_TYPE_SUPERSEDES,
    SOURCE_TYPE_CONNECTOR,
    Memory,
    NeuralMemoryEdge,
)
from models.schemas import RecallRequest, RememberRequest
from repositories.config_repository import ContextSearchConfigRepository
from services.memory_service import MemoryService
from services.quota_service import DailyReservation
from services.search_service import SearchService
from services.user_facts_lane import USER_FACTS_QUERY


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


async def _scope(db, *, lane: bool = True) -> tuple[str, Workspace, Context]:
    owner = f"facts_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"facts-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = await _context(db, owner, ws)
    await _switch(db, ctx.id, on=lane)
    return owner, ws, ctx


async def _context(db, owner: str, ws: Workspace) -> Context:
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"facts-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()
    await db.commit()
    return ctx


async def _switch(db, context_id: UUID, *, on: bool) -> None:
    config = await ContextSearchConfigRepository(db).create_or_get(context_id)
    config.user_facts_lane = on
    await db.commit()


async def _remember(db, owner, ws, ctx, summary: str, content: str, type_: str = "fact") -> UUID:
    result = await MemoryService(db).remember(
        RememberRequest(summary=summary, content=content, type=type_, importance=0.6),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    return result.memory_id


class _Search:
    """``SearchService.hybrid_search`` for both instances (recall's and the lane's)."""

    def __init__(self, main: list[UUID], lane: list[UUID]):
        self.pools = {"main": main, "lane": lane}
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _self, *, query: str, **kwargs):
        self.calls.append({"query": query, **kwargs})
        pool = self.pools["lane" if query == USER_FACTS_QUERY else "main"]
        return [{"id": str(m), "score": 0.9 - 0.05 * i} for i, m in enumerate(pool)]

    @property
    def lane_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["query"] == USER_FACTS_QUERY]


def _patched(search: _Search):
    async def fake(_svc, **kwargs):
        return await search(_svc, **kwargs)

    return patch.object(SearchService, "hybrid_search", new=fake)


async def _run(db, owner, ws, ctx, search: _Search, *, opt_in: bool = True, **request_kw):
    with _patched(search):
        return await MemoryService(db).recall(
            RecallRequest(query="how do I carry the bags upstairs", **request_kw),
            user_id=owner,
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
            user_facts_lane=opt_in,
        )


@pytest.mark.asyncio
async def test_an_unrelated_fact_comes_back_under_user_facts(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    bags = await _remember(db_session, owner, ws, ctx, "Shopping list", "Bags of rice", "note")
    knee = await _remember(
        db_session, owner, ws, ctx, "The user injured a knee", "Stairs hurt the knee."
    )
    back = await _remember(db_session, owner, ws, ctx, "The user has a bad back", "No lifting.")
    third = await _remember(db_session, owner, ws, ctx, "The user is vegetarian", "No meat.")
    search = _Search(main=[bags], lane=[knee, back, third])

    response = await _run(db_session, owner, ws, ctx, search, k=5)

    assert [r.memory_id for r in response.results] == [bags]
    assert [r.memory_id for r in response.user_facts] == [knee, back]  # at most 2
    first = response.user_facts[0]
    assert first.content == "Stairs hurt the knee."
    assert first.created_at is not None and first.type == "fact"
    # One lane search: the fixed query, type fact, the same context, no rerank.
    (lane,) = search.lane_calls
    assert lane["filters"] == {"type": "fact"}
    assert lane["context_id"] == str(ctx.id)
    assert lane["use_rerank"] is False
    assert lane["k"] == 2
    # Who may read what: the same workspace, the same shared-read routing (the
    # vector store's author filter for a private context follows from it).
    main = search.calls[0]
    assert lane["workspace_id"] == main["workspace_id"]
    assert lane["is_shared_context_read"] == main["is_shared_context_read"]


@pytest.mark.asyncio
async def test_a_fact_in_results_is_not_repeated(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    knee = await _remember(db_session, owner, ws, ctx, "The user injured a knee", "Stairs.")
    back = await _remember(db_session, owner, ws, ctx, "The user has a bad back", "No lifting.")
    third = await _remember(db_session, owner, ws, ctx, "The user is vegetarian", "No meat.")
    search = _Search(main=[knee], lane=[knee, back, third])

    response = await _run(db_session, owner, ws, ctx, search, k=5)

    assert [r.memory_id for r in response.results] == [knee]
    assert [r.memory_id for r in response.user_facts] == [back, third]
    # The page's one fact is fetched past.
    assert search.lane_calls[0]["k"] == 3


@pytest.mark.asyncio
async def test_switch_off_runs_no_lane_and_sets_no_key(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session, lane=False)
    knee = await _remember(db_session, owner, ws, ctx, "The user injured a knee", "Stairs.")
    search = _Search(main=[knee], lane=[knee])

    response = await _run(db_session, owner, ws, ctx, search)

    assert len(search.calls) == 1
    assert search.lane_calls == []
    assert response.user_facts is None
    assert "user_facts" not in response.model_dump(exclude_none=True)


@pytest.mark.asyncio
async def test_a_caller_that_does_not_opt_in_gets_no_lane(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    knee = await _remember(db_session, owner, ws, ctx, "The user injured a knee", "Stairs.")
    search = _Search(main=[], lane=[knee])

    response = await _run(db_session, owner, ws, ctx, search, opt_in=False)

    assert search.lane_calls == []
    assert response.user_facts is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_kw",
    [{"filters": {"type": "fact"}}, {"filters": {"type": "note"}}, {"order": "date"}],
)
async def test_type_filter_and_date_order_skip_the_lane(db_session, quiet_write, request_kw):
    owner, ws, ctx = await _scope(db_session)
    knee = await _remember(db_session, owner, ws, ctx, "The user injured a knee", "Stairs.")
    search = _Search(main=[knee], lane=[knee])

    response = await _run(db_session, owner, ws, ctx, search, **request_kw)

    assert search.lane_calls == []
    assert response.user_facts is None


@pytest.mark.asyncio
async def test_an_empty_main_page_still_gets_the_lane(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    knee = await _remember(db_session, owner, ws, ctx, "The user injured a knee", "Stairs.")
    search = _Search(main=[], lane=[knee])

    response = await _run(db_session, owner, ws, ctx, search)

    assert response.results == []
    assert [r.memory_id for r in response.user_facts] == [knee]


@pytest.mark.asyncio
async def test_superseded_other_context_and_non_fact_rows_stay_out(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    old = await _remember(db_session, owner, ws, ctx, "The user injured a knee", "Old.")
    new = await _remember(db_session, owner, ws, ctx, "The user's knee has healed", "New.")
    note = await _remember(
        db_session, owner, ws, ctx, "A note about the garden", "Not a fact.", "note"
    )
    other_ctx = await _context(db_session, owner, ws)
    foreign = await _remember(db_session, owner, ws, other_ctx, "The user is tall", "Other.")
    # Another workspace altogether.
    o2, ws2, ctx2 = await _scope(db_session)
    alien = await _remember(db_session, o2, ws2, ctx2, "Another user is ill", "Elsewhere.")
    db_session.add(
        NeuralMemoryEdge(
            user_id=owner,
            src_id=new,
            dst_id=old,
            workspace_id=ws.id,
            context_id=ctx.id,
            edge_type=EDGE_TYPE_SUPERSEDES,
            weight=1.0,
            origin=EDGE_ORIGIN_DECLARED,
        )
    )
    await db_session.commit()
    search = _Search(main=[], lane=[foreign, alien, old, note, new])

    response = await _run(db_session, owner, ws, ctx, search)

    assert [r.memory_id for r in response.user_facts] == [new]


@pytest.mark.asyncio
async def test_the_trusted_filter_holds_in_the_lane(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    imported = await _remember(db_session, owner, ws, ctx, "The user likes tea", "Imported.")
    stated = await _remember(db_session, owner, ws, ctx, "The user injured a knee", "Stated.")
    await db_session.execute(
        update(Memory).where(Memory.id == imported).values(source_type=SOURCE_TYPE_CONNECTOR)
    )
    await db_session.commit()
    search = _Search(main=[], lane=[imported, stated])

    response = await _run(db_session, owner, ws, ctx, search, filters={"trust_tier": "trusted"})

    assert [r.memory_id for r in response.user_facts] == [stated]
    assert search.lane_calls[0]["filters"] == {"trust_tier": "trusted", "type": "fact"}


@pytest.mark.asyncio
async def test_topic_filters_do_not_narrow_the_lane(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    knee = await _remember(db_session, owner, ws, ctx, "The user injured a knee", "Stairs.")
    search = _Search(main=[], lane=[knee])

    await _run(
        db_session, owner, ws, ctx, search, filters={"tags": ["shopping"], "importance": 0.9}
    )

    assert search.lane_calls[0]["filters"] == {"type": "fact"}


@pytest.mark.asyncio
async def test_the_lane_has_no_side_effects(db_session, quiet_write):
    """Access stats, promotion, supersede suggestions, audit row, Hebbian pass:
    only the main page's rows are touched."""
    owner, ws, ctx = await _scope(db_session)
    bags = await _remember(db_session, owner, ws, ctx, "Shopping list", "Rice.", "note")
    knee = await _remember(db_session, owner, ws, ctx, "The user injured a knee", "Stairs.")
    search = _Search(main=[bags], lane=[knee])
    record = AsyncMock()
    hebbian = AsyncMock()
    resolve = AsyncMock(return_value={})
    emit = AsyncMock()
    writer = MagicMock()
    with (
        patch.object(MemoryService, "_recall_record_access", new=record),
        patch.object(MemoryService, "_recall_run_hebbian_learning", new=hebbian),
        patch.object(MemoryService, "_resolve_supersede_candidates", new=resolve),
        patch("services.memory_access_event_writer.emit_memory_access_event", new=emit),
        patch("services.search_service.LLMCallLogWriter", writer),
    ):
        response = await _run(db_session, owner, ws, ctx, search)

    assert [r.memory_id for r in response.user_facts] == [knee]
    ((args, _kw),) = [(c.args, c.kwargs) for c in record.await_args_list]
    assert [r.memory_id for r in args[0]] == [bags]
    for call in hebbian.await_args_list:
        assert knee not in {r.get("id") for r in call.args[1]}
    for call in resolve.await_args_list:
        assert all(m.id != knee for m in call.args[0].values())
    emit.assert_awaited_once()
    assert emit.await_args.kwargs["result_count"] == 1
    writer.assert_not_called()
    stored = (
        await db_session.execute(select(Memory.access_count).where(Memory.id == knee))
    ).scalar_one()
    assert stored == 0
    assert response.user_facts[0].supersede_candidate is None


@pytest.mark.asyncio
async def test_an_error_in_the_lane_returns_the_page_without_the_key(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    bags = await _remember(db_session, owner, ws, ctx, "Shopping list", "Rice.", "note")
    search = _Search(main=[bags], lane=[])

    async def broken(_svc, *, query, **kwargs):
        if query == USER_FACTS_QUERY:
            raise RuntimeError("vector store down")
        return await search(_svc, query=query, **kwargs)

    with (
        patch.object(SearchService, "hybrid_search", new=broken),
        patch("services.user_facts_lane.logger") as log,
    ):
        response = await MemoryService(db_session).recall(
            RecallRequest(query="bags"),
            user_id=owner,
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
            user_facts_lane=True,
        )

    assert [r.memory_id for r in response.results] == [bags]
    assert response.user_facts is None
    log.warning.assert_called_once()
    assert log.warning.call_args.args == ("user_facts_lane_failed",)
    assert set(log.warning.call_args.kwargs) == {"context_id", "error_class"}
    # The session the caller goes on with still works.
    assert (await db_session.execute(select(func.count()).select_from(Memory))).scalar() >= 1
