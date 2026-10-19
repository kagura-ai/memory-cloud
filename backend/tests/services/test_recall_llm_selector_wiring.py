"""#2144: where the recall LLM selector runs, and where it never does.

Drives the real ``MemoryService.recall`` (IO boundaries stubbed) and the real
gate method: the stage runs only for the MCP tool (``llm_selector=True``) on a
single-context recall whose flag is effective with content on the page; every
other caller and mode is pinned off, and a stored-on flag the plan does not
cover reports ``inactive``.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import MemoryResponse, RecallRequest, RecallResponse, SelectorReport, SpanRef
from services.memory_service import MemoryService, _recall_audit_metadata
from services.recall_selector import SelectorResult
from services.recall_timings import RecallTimings


def _row(content: str | None = "Body sentence long enough to be its own segment here.") -> Any:
    return MemoryResponse(
        memory_id=uuid4(),
        summary="s",
        context_summary=None,
        type="note",
        importance=0.5,
        scope="persistent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        client="t",
        tags=[],
        context=None,
        score=0.5,
        content=content,
    )


def _selected(page: list[MemoryResponse]) -> SelectorResult:
    kept = page[0].model_copy(update={"content": "Body", "spans": [SpanRef(start=0, end=4)]})
    return SelectorResult(
        responses=[kept],
        report=SelectorReport(
            applied=True,
            model="m",
            kept=1,
            dropped=len(page) - 1,
            dropped_memory_ids=[r.memory_id for r in page[1:]],
        ),
    )


async def _gate(
    *,
    page: list[MemoryResponse] | None = None,
    llm_selector: bool = True,
    search_config: Any = "on",
    cross_context: bool = False,
    selection_config: Any = None,
    degradation: dict | None = None,
    effective: bool = True,
    **request_kwargs: Any,
) -> tuple[list[MemoryResponse], SelectorReport | None, AsyncMock, AsyncMock]:
    page = page if page is not None else [_row(), _row()]
    request_kwargs.setdefault("full", 3)
    svc = MemoryService(MagicMock())
    run = AsyncMock(side_effect=lambda *_a, **kw: _selected(kw["responses"]))
    gate = AsyncMock(return_value=effective)
    if search_config == "on":
        search_config = SimpleNamespace(llm_selector_enabled=True)
    with (
        patch("services.recall_selector.run_selector", new=run),
        patch("services.llm_selector_policy.llm_selector_effective", new=gate),
    ):
        responses, report = await svc._recall_apply_llm_selector(
            RecallRequest(query="q", **request_kwargs),
            page,
            llm_selector=llm_selector,
            search_config=search_config,
            cross_context=cross_context,
            selection_config=selection_config,
            degradation=degradation or {},
            user_id="u",
            context_id=uuid4(),
            effective_workspace_id=uuid4(),
            timings=RecallTimings(),
        )
    return responses, report, run, gate


@pytest.mark.asyncio
class TestGate:
    async def test_runs_with_opt_in_flag_and_content(self) -> None:
        page = [_row(), _row()]
        responses, report, run, gate = await _gate(page=page)
        run.assert_awaited_once()
        assert report is not None and report.applied is True
        assert responses[0].spans == [SpanRef(start=0, end=4)]
        assert gate.await_args.kwargs == {"stored": True}
        assert run.await_args.kwargs["responses"] is page

    @pytest.mark.parametrize(
        "overrides",
        [
            {"llm_selector": False},
            {"cross_context": True},
            {"search_config": None},
            {"search_config": SimpleNamespace(llm_selector_enabled=False)},
            {"full": 0},
            {"full": None},
            {"page": [_row(None), _row(None)]},
            {"include_superseded": True},
            {"selection_config": object()},
            {"degradation": {"degraded": True}},
        ],
    )
    async def test_skipped_without_a_report(self, overrides: dict) -> None:
        page = overrides.pop("page", None) or [_row(), _row()]
        responses, report, run, gate = await _gate(page=page, **overrides)
        assert responses is page and report is None
        run.assert_not_called()
        gate.assert_not_called()

    async def test_stored_on_but_not_effective_is_inactive(self) -> None:
        page = [_row()]
        responses, report, run, _ = await _gate(page=page, effective=False)
        assert responses is page
        assert report == SelectorReport(applied=False, reason="inactive")
        run.assert_not_called()

    async def test_the_effective_workspace_pays(self) -> None:
        svc = MemoryService(MagicMock())
        ws, ctx = uuid4(), uuid4()
        run = AsyncMock(side_effect=lambda *_a, **kw: _selected(kw["responses"]))
        with (
            patch("services.recall_selector.run_selector", new=run),
            patch(
                "services.llm_selector_policy.llm_selector_effective",
                new=AsyncMock(return_value=True),
            ),
        ):
            await svc._recall_apply_llm_selector(
                RecallRequest(query="q", full=3),
                [_row()],
                llm_selector=True,
                search_config=SimpleNamespace(llm_selector_enabled=True),
                cross_context=False,
                selection_config=None,
                degradation={},
                user_id="u",
                context_id=ctx,
                effective_workspace_id=ws,
                timings=RecallTimings(),
            )
        kwargs = run.await_args.kwargs
        assert (kwargs["workspace_id"], kwargs["context_id"]) == (str(ws), str(ctx))
        assert kwargs["query"] == "q"


@pytest.mark.asyncio
async def test_real_recall_ships_the_selected_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """The selected page is what access stats, the audit row and the response see."""
    from services import memory_service as ms

    log = MagicMock()
    monkeypatch.setattr(ms, "logger", log)
    service = MemoryService(db=AsyncMock())
    ctx, ws = uuid4(), uuid4()
    request = RecallRequest(query="q", full=3)
    plan = SimpleNamespace(
        request=request,
        effective_workspace_id=ws,
        is_shared_context_read=False,
        neural_enabled=True,
        search_config=SimpleNamespace(llm_selector_enabled=True),
        search_context_id=str(ctx),
    )
    page = [_row(), _row(), _row()]
    # Three page rows plus one candidate beyond the page (k=5 > 3 rows here is
    # irrelevant: the page builder is stubbed).
    beyond = uuid4()
    memories = {f"m{i}": SimpleNamespace(id=r.memory_id) for i, r in enumerate(page)}
    memories["m3"] = SimpleNamespace(id=beyond)
    pool = [{"id": f"m{i}", "score": 0.9 - 0.1 * i} for i in range(4)]
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
    emit = AsyncMock()
    run = AsyncMock(side_effect=lambda *_a, **kw: _selected(kw["responses"]))
    with (
        patch("services.recall_selector.run_selector", new=run),
        patch(
            "services.llm_selector_policy.llm_selector_effective",
            new=AsyncMock(return_value=True),
        ),
        patch("services.memory_access_event_writer.emit_memory_access_event", new=emit),
    ):
        result = await service.recall(
            request,
            "u1",
            current_context_id=ctx,
            current_workspace_id=ws,
            capacity_gate=False,
            llm_selector=True,
        )

    assert isinstance(result, RecallResponse)
    assert [r.memory_id for r in result.results] == [page[0].memory_id]
    assert result.selector is not None and result.selector.dropped == 2
    assert result.selector.dropped_memory_ids == [page[1].memory_id, page[2].memory_id]
    shipped = service._recall_record_access.await_args.args[0]
    assert [r.memory_id for r in shipped] == [page[0].memory_id]
    audit = emit.await_args.kwargs
    assert audit["result_count"] == 1
    assert audit["extra_metadata"]["selector_applied"] is True
    assert audit["extra_metadata"]["selector_dropped_count"] == 2
    assert audit["extra_metadata"]["content_memory_ids"] == [str(page[0].memory_id)]
    # The dropped rows leave the pool Hebbian learning and confidence read;
    # the candidate beyond the page (never judged) stays.
    hebbian_pool = service._recall_run_hebbian_learning.await_args.args[1]
    assert [sr["id"] for sr in hebbian_pool] == ["m0", "m3"]
    assert result.confidence is not None and result.confidence.result_count == 2
    completed = [c for c in log.info.call_args_list if c.args[0] == "recall_completed"]
    assert "select_ms" in completed[0].kwargs and completed[0].kwargs["results"] == 1


@pytest.mark.asyncio
async def test_real_recall_without_opt_in_never_reads_the_flag() -> None:
    service = MemoryService(db=AsyncMock())
    with patch("services.recall_selector.run_selector", new=AsyncMock()) as run:
        _, report = await service._recall_apply_llm_selector(
            RecallRequest(query="q", full=3),
            [_row()],
            llm_selector=False,
            search_config=SimpleNamespace(llm_selector_enabled=True),
            cross_context=False,
            selection_config=None,
            degradation={},
            user_id="u",
            context_id=uuid4(),
            effective_workspace_id=uuid4(),
            timings=RecallTimings(),
        )
    assert report is None
    run.assert_not_called()


# ------------------------------------------------- callers pinned off (C8)


def test_the_service_default_is_off() -> None:
    assert inspect.signature(MemoryService.recall).parameters["llm_selector"].default is False


def test_forget_by_query_never_opts_in() -> None:
    assert "llm_selector" not in inspect.getsource(MemoryService.forget)


@pytest.mark.asyncio
async def test_rest_recall_never_opts_in() -> None:
    from api.routes.memory import recall as rest_recall

    service = MagicMock()
    service.recall = AsyncMock(return_value=RecallResponse(results=[]))
    await rest_recall(
        RecallRequest(query="q", full=2),
        {"user_id": "u", "current_workspace_id": uuid4()},
        service,
        db=MagicMock(),
    )
    assert service.recall.await_args.kwargs.get("llm_selector", False) is False


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
        await share_recall(RecallRequest(query="q", full=5), principal, service)
    assert service.recall.await_args.kwargs.get("llm_selector", False) is False


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
    assert kwargs.get("llm_selector", False) is False


def test_mcp_bootstrap_never_opts_in() -> None:
    """The MCP bootstrap tool composes its recall through other services."""
    from mcp_server.tools import bootstrap as bootstrap_tool

    assert "llm_selector" not in inspect.getsource(bootstrap_tool)


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
    assert captured["llm_selector"] is True


# ------------------------------------------------------------ audit metadata


def test_audit_metadata_counts_only_when_applied() -> None:
    rows = [_row()]
    applied = SelectorReport(applied=True, model="m", kept=1, dropped=3, dropped_memory_ids=[])
    meta = _recall_audit_metadata(rows, 0, applied)
    assert meta is not None
    assert meta["selector_applied"] is True and meta["selector_dropped_count"] == 3
    refused = _recall_audit_metadata(rows, 0, SelectorReport(applied=False, reason="timeout"))
    assert refused is not None and "selector_applied" not in refused
    assert "selector_applied" not in (_recall_audit_metadata(rows, 0) or {})


@pytest.mark.asyncio
async def test_a_page_emptied_by_the_selector_gets_no_tag_suggestions() -> None:
    svc = MemoryService(MagicMock())
    svc._tag_suggestions_for_empty_result = AsyncMock(return_value={"auth": ["oauth (3)"]})
    request = RecallRequest(query="q", full=3, filters={"tags": ["auth"]})
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()):
        emptied = await svc._recall_finalize_response(
            request,
            [],
            [],
            user_id="u",
            current_context_id=uuid4(),
            effective_workspace_id=uuid4(),
            neural_enabled=False,
            binding_row_filtered=0,
            selection_evidence=None,
            cross_context=False,
            degradation=None,
            selector=SelectorReport(
                applied=True, model="m", kept=0, dropped=2, dropped_memory_ids=[]
            ),
        )
        plain = await svc._recall_finalize_response(
            request,
            [],
            [],
            user_id="u",
            current_context_id=uuid4(),
            effective_workspace_id=uuid4(),
            neural_enabled=False,
            binding_row_filtered=0,
            selection_evidence=None,
            cross_context=False,
            degradation=None,
        )
    assert emptied.tag_suggestions is None
    assert plain.tag_suggestions == {"auth": ["oauth (3)"]}
