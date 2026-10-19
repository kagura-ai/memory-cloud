"""The recall LLM selector on Postgres (#2144).

Real memories written through ``MemoryService.remember()`` and read back by
``MemoryService.recall(llm_selector=True)`` on a context whose search config
has ``llm_selector_enabled`` on. Stubbed at the module boundary only: the
embedding task, the Redis quota and day counter, ``hybrid_search`` (a fixed
candidate order), the judge config read and the provider call. Hydration, the
page builder, the selection, the access-stat write and the ``llm_call_log``
row run on the database.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.llm_call_log import LLMCallLog
from models.memory import Memory
from models.schemas import RecallRequest, RememberRequest
from repositories.config_repository import ContextSearchConfigRepository
from services import recall_selector as rs
from services.llm_service import LLMResponse
from services.memory_service import MemoryService
from services.quota_service import DailyReservation

BODY = (
    "The deploy pipeline runs on every merge to main and takes about nine minutes. "
    "Rollbacks use the previous image tag and never rebuild from source code. "
    "Database migrations run before the new containers start serving traffic. "
)


@pytest.fixture
def quiet_write():
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


async def _scope(db, *, selector_on: bool = True) -> tuple[str, Workspace, Context]:
    owner = f"sel_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"sel-ws-{uuid4().hex[:8]}",
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
        name=f"sel-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()
    config = await ContextSearchConfigRepository(db).create_or_get(ctx.id)
    config.llm_selector_enabled = selector_on
    await db.commit()
    return owner, ws, ctx


async def _remember(db, owner, ws, ctx, summary: str, content: str) -> UUID:
    result = await MemoryService(db).remember(
        RememberRequest(summary=summary, content=content, type="fact", importance=0.6),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    return result.memory_id


def _answer(parsed: dict) -> LLMResponse:
    return LLMResponse(
        parsed=parsed,
        total_tokens=130,
        input_tokens=110,
        output_tokens=20,
        cached_input_tokens=0,
        provider="openai",
        model="small-model",
        cache_write_tokens=0,
    )


async def _recall(
    db, owner, ws, ctx, ids, complete_json, *, llm_selector=True, content_trusted_only=False
):
    svc = MemoryService(db)
    svc.search_service.hybrid_search = AsyncMock(
        return_value=[{"id": str(m), "score": 0.9 - 0.1 * i} for i, m in enumerate(ids)]
    )
    config = SimpleNamespace(
        sleep_llm_provider="openai",
        sleep_llm_model="small-model",
        sleep_llm_from_managed_lane=False,
    )
    with (
        patch.object(rs, "_load_judge_config", new=AsyncMock(return_value=config)),
        patch("db.redis.incrby_counter", new=AsyncMock(return_value=1)),
        patch("services.llm_service.LLMService.complete_json", new=complete_json),
    ):
        return await svc.recall(
            RecallRequest(query="how do rollbacks work", k=5, full=3),
            user_id=owner,
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
            llm_selector=llm_selector,
            content_trusted_only=content_trusted_only,
        )


@pytest.mark.asyncio
async def test_selected_page_is_verbatim_and_leaves_one_ledger_row(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    ids = [
        await _remember(db_session, owner, ws, ctx, "Deploy pipeline facts", BODY),
        await _remember(db_session, owner, ws, ctx, "Office lunch menu", "Pasta on Fridays."),
        await _remember(db_session, owner, ws, ctx, "Rollback notes", BODY),
    ]
    complete_json = AsyncMock(
        return_value=_answer(
            {
                "rows": [
                    {"i": 0, "relevant": True, "segments": [1], "content": "INJECTED"},
                    {"i": 1, "relevant": False},
                ]
            }
        )
    )

    response = await _recall(db_session, owner, ws, ctx, ids, complete_json)

    assert complete_json.await_args.kwargs["max_attempts"] == 1
    assert [r.memory_id for r in response.results] == [ids[0], ids[2]]
    assert response.selector is not None
    assert response.selector.applied is True and response.selector.dropped == 1
    assert response.selector.dropped_memory_ids == [ids[1]]
    stored = (await db_session.get(Memory, ids[0])).content
    top = response.results[0]
    assert top.spans is not None and len(top.spans) == 1
    assert top.content == stored[top.spans[0].start : top.spans[0].end]
    assert top.content.startswith("Rollbacks use the previous image tag")
    assert "INJECTED" not in response.model_dump_json()
    # The kept-whole row is unchanged.
    assert response.results[1].content == BODY and response.results[1].spans is None

    rows = (
        (await db_session.execute(select(LLMCallLog).where(LLMCallLog.workspace_id == ws.id)))
        .scalars()
        .all()
    )
    completions = [r for r in rows if r.call_type == "completion"]
    assert len(completions) == 1
    row = completions[0]
    assert row.caller == "recall" and str(row.context_id) == str(ctx.id)
    assert row.call_metadata["stage"] == "llm_selector"
    assert row.call_metadata["outcome"] == "applied"
    assert (row.call_metadata["kept"], row.call_metadata["dropped"]) == (2, 1)
    assert (row.input_tokens, row.output_tokens) == (110, 20)


@pytest.mark.asyncio
async def test_a_dropped_row_is_not_counted_as_surfaced(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    ids = [
        await _remember(db_session, owner, ws, ctx, "Deploy pipeline facts", BODY),
        await _remember(db_session, owner, ws, ctx, "Office lunch menu", "Pasta on Fridays."),
    ]
    complete_json = AsyncMock(return_value=_answer({"rows": [{"i": 1, "relevant": False}]}))
    await _recall(db_session, owner, ws, ctx, ids, complete_json)

    db_session.expire_all()
    kept = await db_session.get(Memory, ids[0])
    dropped = await db_session.get(Memory, ids[1])
    assert kept.access_count == 1
    assert dropped.access_count == 0


@pytest.mark.asyncio
async def test_flag_off_or_no_opt_in_never_calls_the_model(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session, selector_on=False)
    ids = [await _remember(db_session, owner, ws, ctx, "Deploy pipeline facts", BODY)]
    complete_json = AsyncMock()
    response = await _recall(db_session, owner, ws, ctx, ids, complete_json)
    assert response.selector is None
    assert response.results[0].content == BODY

    owner, ws, ctx = await _scope(db_session, selector_on=True)
    ids = [await _remember(db_session, owner, ws, ctx, "Deploy pipeline facts", BODY)]
    response = await _recall(db_session, owner, ws, ctx, ids, complete_json, llm_selector=False)
    assert response.selector is None
    complete_json.assert_not_called()


@pytest.mark.asyncio
async def test_plan_gate_reports_inactive(db_session, quiet_write, monkeypatch):
    """With the gate on, a free-plan workspace's stored flag is not acted on."""
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "llm_selector_require_plan", True)
    owner, ws, ctx = await _scope(db_session)
    ws_row = await db_session.get(Workspace, ws.id)
    ws_row.plan_name = "free"
    await db_session.commit()
    ids = [await _remember(db_session, owner, ws, ctx, "Deploy pipeline facts", BODY)]
    complete_json = AsyncMock()
    response = await _recall(db_session, owner, ws, ctx, ids, complete_json)
    assert response.selector is not None
    assert response.selector.model_dump(exclude_none=True) == {
        "applied": False,
        "reason": "inactive",
    }
    complete_json.assert_not_called()
    assert response.results[0].content == BODY


@pytest.mark.asyncio
async def test_defaulted_full_never_sends_connector_content(db_session, quiet_write):
    """C2 / #2124: with the MCP tool's defaulted full, a connector-ingested row
    carries no content, so the model sees it summary-only — its stored text
    never enters the prompt."""
    from sqlalchemy import update

    from models.memory import SOURCE_TYPE_CONNECTOR

    owner, ws, ctx = await _scope(db_session)
    secret = "CONNECTOR-BODY-7d1e must never reach the selector prompt at all."
    ids = [
        await _remember(db_session, owner, ws, ctx, "External ticket text", secret),
        await _remember(db_session, owner, ws, ctx, "Deploy pipeline facts", BODY),
    ]
    await db_session.execute(
        update(Memory).where(Memory.id == ids[0]).values(source_type=SOURCE_TYPE_CONNECTOR)
    )
    await db_session.commit()
    complete_json = AsyncMock(return_value=_answer({"rows": [{"i": 1, "relevant": True}]}))

    response = await _recall(
        db_session, owner, ws, ctx, ids, complete_json, content_trusted_only=True
    )

    prompt = complete_json.await_args.kwargs["prompt"]
    assert "CONNECTOR-BODY-7d1e" not in prompt
    assert "Row 0 (summary only)" in prompt
    assert "[0] The deploy pipeline" in prompt
    assert response.results[0].content is None


@pytest.mark.asyncio
async def test_the_real_judge_config_read_and_release_commit(db_session, quiet_write, monkeypatch):
    """Nothing patched between recall and the provider call: the judge config is
    read from the database / env in a savepoint on the recall's session, the
    session is committed before the round trip, and the page still ships."""
    from neural.config import NeuralMemoryConfig

    monkeypatch.setenv("SLEEP_LLM_PROVIDER", "openai")
    monkeypatch.setenv("SLEEP_LLM_MODEL", "small-model")
    NeuralMemoryConfig._db_cache.clear()
    owner, ws, ctx = await _scope(db_session)
    ids = [
        await _remember(db_session, owner, ws, ctx, "Deploy pipeline facts", BODY),
        await _remember(db_session, owner, ws, ctx, "Office lunch menu", "Pasta on Fridays."),
    ]
    complete_json = AsyncMock(return_value=_answer({"rows": [{"i": 1, "relevant": False}]}))
    svc = MemoryService(db_session)
    svc.search_service.hybrid_search = AsyncMock(
        return_value=[{"id": str(m), "score": 0.9 - 0.1 * i} for i, m in enumerate(ids)]
    )
    try:
        with (
            patch("db.redis.incrby_counter", new=AsyncMock(return_value=1)),
            patch("services.llm_service.LLMService.complete_json", new=complete_json),
        ):
            response = await svc.recall(
                RecallRequest(query="deploy", k=5, full=3),
                user_id=owner,
                current_context_id=ctx.id,
                current_workspace_id=ws.id,
                llm_selector=True,
            )
    finally:
        NeuralMemoryConfig._db_cache.clear()

    assert complete_json.await_args.kwargs["provider"] == "openai"
    assert complete_json.await_args.kwargs["model"] == "small-model"
    assert response.selector is not None and response.selector.applied is True
    assert [r.memory_id for r in response.results] == [ids[0]]
