"""#2754: who gets the recall ``hint`` and which candidates it counts.

The hint names the MCP ``list`` tool, so it is opt-in per caller exactly like
``llm_selector`` and ``related_index``: only the MCP recall tool passes
``list_hint=True``. Share-key, REST, bootstrap and ``forget(query=...)`` never
compute it, and ``RecallResponse`` never serialises it into a REST body.

Driven through the real ``MemoryService.recall`` (IO boundaries stubbed): the
pool it counts is the query's own candidates minus the rows the LLM selector
dropped and minus the related-index extras, which are not hits for the query.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.settings import get_settings
from models.schemas import (
    MemoryResponse,
    RecallRequest,
    RecallResponse,
    SelectorReport,
)
from services.memory_service import MemoryService
from services.recall_selector import SelectorResult

SRC = Path(__file__).resolve().parents[2] / "src"


def _row(tags: list[str]) -> MemoryResponse:
    return MemoryResponse(
        memory_id=uuid4(),
        summary="s",
        context_summary=None,
        type="note",
        importance=0.5,
        scope="persistent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        client="t",
        tags=tags,
        context=None,
        score=0.5,
        content="Body sentence long enough to be its own segment here.",
    )


def _hint(tag: str, matched: int, shown: int) -> str:
    return (
        f'At least {matched} memories matching this search carry tag "{tag}", '
        f'{shown} shown; list(filters={{"tags": ["{tag}"]}}) returns every memory with it.'
    )


@pytest.fixture
def hint_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "recall_list_hint_enabled", True)


def _service(page: list[MemoryResponse], beyond_tags: list[list[str]]) -> MemoryService:
    """A recall whose pool is ``page`` then one candidate per ``beyond_tags``."""
    service = MemoryService(db=AsyncMock())
    request = RecallRequest(query="q", full=3)
    plan = SimpleNamespace(
        request=request,
        effective_workspace_id=uuid4(),
        is_shared_context_read=False,
        neural_enabled=True,
        search_config=SimpleNamespace(llm_selector_enabled=True),
        search_context_id=str(uuid4()),
    )
    memories: dict[str, Any] = {
        f"m{i}": SimpleNamespace(id=r.memory_id, tags=r.tags) for i, r in enumerate(page)
    }
    for j, tags in enumerate(beyond_tags, start=len(page)):
        memories[f"m{j}"] = SimpleNamespace(id=uuid4(), tags=tags)
    pool = [{"id": key, "score": 0.9 - 0.01 * i} for i, key in enumerate(memories)]
    service._recall_check_agent_bindings = AsyncMock()
    service._recall_prepare = AsyncMock(return_value=plan)
    service._recall_resolve_cluster_filter = AsyncMock(return_value=None)
    service.search_service = MagicMock()
    service.search_service.hybrid_search = AsyncMock(return_value=pool)
    service._recall_hydrate_memories = AsyncMock(return_value=memories)
    service._recall_filter_rows_by_binding = AsyncMock(side_effect=lambda _r, m, **_k: (m, 0))
    service._apply_supersede_shadowing = AsyncMock(return_value=({}, {}))
    service._apply_supersede_fold = AsyncMock(return_value={})
    service._maybe_reinforce_rerank = AsyncMock()
    service._maybe_graph_boost = AsyncMock()
    service._recall_build_responses = AsyncMock(return_value=page)
    service._recall_record_access = AsyncMock()
    service._recall_run_hebbian_learning = AsyncMock()
    service._tag_suggestions_for_empty_result = AsyncMock(return_value=None)
    return service


async def _recall(service: MemoryService, **kwargs: Any) -> RecallResponse:
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()):
        return await service.recall(
            RecallRequest(query="q", full=3),
            "u1",
            current_context_id=uuid4(),
            current_workspace_id=uuid4(),
            capacity_gate=False,
            **kwargs,
        )


# ------------------------------------------------------ the real recall path


@pytest.mark.asyncio
class TestTheRealRecall:
    async def test_the_opt_in_caller_gets_it(self, hint_on: None) -> None:
        page = [_row(["trip"])]
        result = await _recall(_service(page, [["trip"], ["trip"]]), list_hint=True)
        assert result.hint == _hint("trip", 3, 1)

    async def test_a_caller_that_does_not_opt_in_never_gets_it(self, hint_on: None) -> None:
        page = [_row(["trip"])]
        result = await _recall(_service(page, [["trip"], ["trip"]]))
        assert result.hint is None

    async def test_rows_the_selector_dropped_do_not_count(self, hint_on: None) -> None:
        page = [_row(["trip"]), _row(["trip"]), _row(["trip"])]

        def selected(*_a: Any, **kw: Any) -> SelectorResult:
            rows = kw["responses"]
            return SelectorResult(
                responses=[rows[0]],
                report=SelectorReport(
                    applied=True,
                    model="m",
                    kept=1,
                    dropped=2,
                    dropped_memory_ids=[r.memory_id for r in rows[1:]],
                ),
            )

        with (
            patch("services.recall_selector.run_selector", new=AsyncMock(side_effect=selected)),
            patch(
                "services.llm_selector_policy.llm_selector_effective",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await _recall(_service(page, [["trip"]]), llm_selector=True, list_hint=True)
        # The pool without the two dropped rows: the shown row + the one beyond.
        assert result.hint == _hint("trip", 2, 1)

    async def test_related_index_extras_do_not_count(self, hint_on: None) -> None:
        page = [_row(["trip"])]
        service = _service(page, [["trip"]])
        extras = {f"x{i}": SimpleNamespace(id=uuid4(), tags=["trip"]) for i in range(5)}
        extra_hits = [{"id": key, "score": 0.1} for key in extras]
        with (
            patch(
                "services.recall_related_index.expand_recall",
                new=AsyncMock(return_value=(extra_hits, extras)),
            ),
            patch("services.sleep.related_index.index_enabled", return_value=True),
        ):
            result = await _recall(service, related_index=True, list_hint=True)
        # Two query hits carry the tag; the five linked extras are not hits.
        assert result.hint == _hint("trip", 2, 1)


# ------------------------------------------------------- callers pinned off


def test_the_service_default_is_off() -> None:
    assert inspect.signature(MemoryService.recall).parameters["list_hint"].default is False


def test_only_the_mcp_recall_tool_opts_in() -> None:
    callers = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "list_hint=True" in path.read_text(encoding="utf-8")
    }
    assert callers == {"mcp_server/tools/memory.py"}


def test_forget_by_query_never_opts_in() -> None:
    assert "list_hint" not in inspect.getsource(MemoryService.forget)


@pytest.mark.asyncio
async def test_the_mcp_recall_tool_opts_in() -> None:
    from mcp_server.tools import memory as mcp_memory
    from tests.services.test_recall_full_content import _get_db_yielding

    captured: dict[str, Any] = {}

    async def recall(request: Any, **kwargs: Any) -> RecallResponse:
        captured.update(kwargs)
        return RecallResponse(results=[])

    service = MagicMock()
    service.recall = recall
    with (
        patch("db.base.get_db", new=_get_db_yielding(AsyncMock())),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        await mcp_memory.handle_recall({"query": "q", "context_id": str(uuid4())}, "u", None)
    assert captured["list_hint"] is True


@pytest.mark.asyncio
async def test_share_key_recall_never_opts_in() -> None:
    from api.routes.share_keys import share_recall

    service = MagicMock()
    service.db = MagicMock()
    service.recall = AsyncMock(return_value=RecallResponse(results=[]))
    principal = {
        "share_key_context_id": uuid4(),
        "user_id": "u",
        "current_workspace_id": uuid4(),
    }
    with patch("api.routes.share_keys.ensure_context_not_capacity_locked", new=AsyncMock()):
        await share_recall(RecallRequest(query="q"), principal, service)
    assert service.recall.await_args.kwargs.get("list_hint", False) is False


@pytest.mark.asyncio
async def test_rest_recall_never_opts_in() -> None:
    from api.routes.memory import recall as rest_recall

    service = MagicMock()
    service.recall = AsyncMock(return_value=RecallResponse(results=[]))
    await rest_recall(
        RecallRequest(query="q"),
        {"user_id": "u", "current_workspace_id": uuid4()},
        service,
        db=MagicMock(),
    )
    assert service.recall.await_args.kwargs.get("list_hint", False) is False


@pytest.mark.asyncio
async def test_agent_bootstrap_never_opts_in() -> None:
    from services.agent_bootstrap_service import (
        AgentBootstrapService,
        BootstrapParams,
        BootstrapPrincipal,
    )
    from tests.mcp_server.test_recall_envelope import _context

    svc = AgentBootstrapService(MagicMock())
    with patch("services.memory_service.MemoryService") as ms_cls:
        ms_cls.return_value.recall = AsyncMock(return_value=RecallResponse(results=[]))
        await svc._recall(
            _context(),
            BootstrapParams(agent_id=uuid4(), query="auth"),
            BootstrapPrincipal(user_id="u", workspace_id=uuid4(), principal_type="agent"),
        )
        kwargs = ms_cls.return_value.recall.await_args.kwargs
    assert kwargs.get("list_hint", False) is False


def test_the_rest_schema_has_no_hint_property() -> None:
    """Excluded from serialisation, so neither the body nor the OpenAPI schema has it."""
    properties = RecallResponse.model_json_schema(mode="serialization")["properties"]
    assert "hint" not in properties
    assert "hint" not in RecallResponse(results=[], hint="x").model_dump(mode="json")
