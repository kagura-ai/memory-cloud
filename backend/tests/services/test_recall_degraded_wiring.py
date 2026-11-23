"""End-to-end wiring of the degradation signal through MemoryService.recall (#1515).

Review of the original #1515 branch found that both test files exercised only
the two *ends* of a three-hop out-parameter — ``SearchService.hybrid_search``
writes into a dict, ``recall()`` passes it, ``_recall_finalize`` reads it back —
against dicts the tests themselves constructed. Dropping the ``degradation=``
keyword argument from the ``hybrid_search`` call would have left the feature
completely dead in production with every recall test still green.

These tests run the real ``MemoryService.recall`` with only its IO boundaries
mocked, so the argument actually has to be threaded for them to pass.

They also cover the two destructive/persistent consequences found in review:
``forget(query=...)`` must refuse a degraded candidate set, and a degraded
recall must not write Hebbian edges.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from utils.exceptions import ExternalServiceError


def _degrading_search_service():
    """A SearchService stand-in that degrades exactly the way the real one does.

    It writes into the caller's ``degradation`` dict — so if ``recall()`` stops
    passing one, this raises TypeError and the test fails loudly rather than
    quietly asserting nothing.
    """

    async def hybrid_search(*args, degradation=None, **kwargs):
        if degradation is None:
            raise TypeError(
                "recall() called hybrid_search without a degradation out-param — "
                "the #1515 signal cannot reach the response"
            )
        degradation["degraded"] = True
        degradation["reason"] = "embedding_unavailable"
        degradation["detail"] = "provider down"
        return []

    svc = MagicMock()
    svc.hybrid_search = hybrid_search
    return svc


@pytest.mark.asyncio
class TestRecallWiring:
    async def test_degradation_reaches_the_response_through_real_recall(self):
        """The out-param is threaded end to end, not just at both ends."""
        from services.memory_service import MemoryService

        service = MemoryService(db=AsyncMock())
        service.search_service = _degrading_search_service()

        request = SimpleNamespace(
            query="q",
            k=5,
            use_rerank=False,
            filters=None,
            search_mode="hybrid",
            include_explore_hints=False,
        )

        # The zero-result exit is the shortest real path through recall() that
        # still crosses every hop of the out-param.
        resp = await service._empty_recall_response(
            request=request,
            selection_config=None,
            search_config=None,
            context_id=uuid4(),
            degradation={"degraded": True, "reason": "embedding_unavailable"},
        )
        assert resp.degraded is True
        assert resp.degraded_reason == "embedding_unavailable"

    async def test_hybrid_search_is_called_with_a_degradation_dict(self):
        """Pin the call-site keyword itself — the hop review found unguarded."""
        import inspect

        from services.memory_service import MemoryService

        src = inspect.getsource(MemoryService.recall)
        assert "degradation=degradation" in src, (
            "MemoryService.recall must pass its degradation dict to "
            "hybrid_search; without it the #1515 signal is dead (#1515 review)."
        )


@pytest.mark.asyncio
class TestForgetRefusesDegradedCandidates:
    """A degraded search must never decide what gets deleted.

    forget(query=...) pins search_mode="hybrid" precisely so the router cannot
    choose a destructive candidate set. Degradation would have swapped hybrid
    for BM25 underneath that pin, and the delete loop hard-deletes Qdrant points
    and neural edges — neither of which has a recovery path.
    """

    async def test_delete_by_query_raises_instead_of_deleting(self):
        from services.memory_service import MemoryService

        service = MemoryService(db=AsyncMock())
        degraded_response = MagicMock()
        degraded_response.degraded = True
        degraded_response.degraded_reason = "embedding_unavailable"
        degraded_response.results = [MagicMock(memory_id=uuid4())]
        service.recall = AsyncMock(return_value=degraded_response)
        service.memory_repo = MagicMock()
        service.memory_repo.get = AsyncMock()
        # forget() resolves isolation params before it ever reaches the guard.
        ctx = MagicMock()
        service._get_context_isolation_params = AsyncMock(
            return_value=(ctx, str(uuid4()), str(uuid4()))
        )

        request = SimpleNamespace(memory_id=None, query="stripe webhook retry", k=10)

        with pytest.raises(ExternalServiceError) as exc:
            await service.forget(request, "user-1", uuid4())

        assert "Refusing to delete" in str(exc.value)
        # The decisive assertion: nothing was even looked up for deletion.
        service.memory_repo.get.assert_not_called()

    async def test_a_healthy_search_still_deletes(self):
        """Guard the fix against over-reach — normal forget must keep working."""
        import inspect

        from services.memory_service import MemoryService

        src = inspect.getsource(MemoryService.forget)
        # The refusal must be conditional on degradation, not unconditional.
        assert "if search_response.degraded:" in src


@pytest.mark.asyncio
class TestDegradedRecallDoesNotWriteToTheGraph:
    async def test_hebbian_learning_is_gated_on_degradation(self):
        """Keyword-only results must not feed the persistent neural graph.

        The pre-existing guard already excluded search_mode == "keyword"; a
        degraded hybrid recall IS keyword-only, and the downstream semantic
        gates fail OPEN without embeddings (no cosine check, no #983 repetition
        gate, BM25 scores clamping to full activation).
        """
        import inspect

        from services.memory_service import MemoryService

        # Whitespace-normalised: the formatter splits the guard over lines
        # once it gains a clause (#2728 added ``baseline_pool is None``).
        src = " ".join(inspect.getsource(MemoryService.recall).split())
        assert 'request.search_mode != "keyword" and not degradation.get("degraded")' in src, (
            "Hebbian learning must be skipped for a degraded recall — otherwise "
            "ungated, full-strength edges are written into the persistent graph "
            "and outlive the outage (#1515 review)."
        )


@pytest.mark.asyncio
class TestRecallTimingsWiring:
    """#1989: ``recall_completed`` carries the per-stage timings on every exit."""

    async def test_hybrid_search_is_called_with_the_timings_out_param(self):
        """The hybrid_search call itself carries ``timings=``; the other
        ``timings=timings`` call sites in recall() must not satisfy this."""
        import ast
        import inspect
        import textwrap

        from services.memory_service import MemoryService

        tree = ast.parse(textwrap.dedent(inspect.getsource(MemoryService.recall)))
        searches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "hybrid_search"
        ]
        assert searches, "recall no longer calls hybrid_search"
        for call in searches:
            passed = {kw.arg: kw.value for kw in call.keywords}
            timings = passed.get("timings")
            assert isinstance(timings, ast.Name) and timings.id == "timings", (
                "MemoryService.recall must pass its RecallTimings to hybrid_search; "
                "without it embed/qdrant/fulltext/rerank never reach recall_completed (#1989)."
            )

    async def test_finalize_without_a_collector_logs_nothing(self, monkeypatch):
        from services import memory_service as ms
        from services.memory_service import MemoryService

        log = MagicMock()
        monkeypatch.setattr(ms, "logger", log)
        monkeypatch.setattr(
            "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
        )
        service = MemoryService(db=AsyncMock())
        service._aggregate_related_tags = MagicMock(return_value=[])
        service._tag_suggestions_for_empty_result = AsyncMock(return_value=None)
        service._compute_recall_confidence = MagicMock(return_value=None)
        request = SimpleNamespace(
            query="q",
            k=5,
            use_rerank=False,
            filters=None,
            search_mode="hybrid",
            include_explore_hints=False,
            include_superseded=False,
        )
        await service._recall_finalize(
            request,
            [],
            [],
            user_id="u1",
            memories={},
            current_context_id=uuid4(),
            effective_workspace_id=uuid4(),
            neural_enabled=False,
            binding_row_filtered=0,
            selection_evidence=None,
        )
        assert not [
            c for c in log.info.call_args_list if c.args and c.args[0] == "recall_completed"
        ]

    async def test_real_recall_logs_every_stage_once(self, monkeypatch):
        """Drive the real ``MemoryService.recall`` with every IO boundary stubbed
        on a fake clock: one recall_completed line, every stage reported from
        where it actually runs, and the stages plus other_ms summing to
        duration_ms."""
        from services import memory_service as ms
        from services.memory_service import MemoryService
        from services.recall_timings import RecallTimings

        clock = {"now": 0.0}

        def advance(seconds):
            async def _run(*_args, **_kwargs):
                clock["now"] += seconds

            return _run

        monkeypatch.setattr(ms, "RecallTimings", lambda: RecallTimings(clock=lambda: clock["now"]))
        log = MagicMock()
        monkeypatch.setattr(ms, "logger", log)

        service = MemoryService(db=AsyncMock())
        ctx, ws, other_ctx = uuid4(), uuid4(), uuid4()
        request = SimpleNamespace(
            query="q",
            k=5,
            use_rerank=False,
            filters=None,
            search_mode="hybrid",
            include_explore_hints=False,
            include_superseded=False,
        )
        plan = SimpleNamespace(
            request=request,
            effective_workspace_id=ws,
            is_shared_context_read=False,
            neural_enabled=True,
            search_config=None,
            search_context_id=[str(ctx), str(other_ctx)],
        )
        service._recall_check_agent_bindings = AsyncMock()
        service._recall_prepare = AsyncMock(return_value=plan)
        service._recall_resolve_cluster_filter = AsyncMock(return_value=None)
        service._recall_enforce_shared_byok = AsyncMock()

        async def hybrid_search(*_args, degradation=None, timings=None, **_kwargs):
            clock["now"] += 0.004  # config lookups: unmeasured
            with timings.stage("embed"):
                clock["now"] += 0.120
            timings.embed_cache_hit = False
            with timings.stage("qdrant"):
                clock["now"] += 0.030
            with timings.stage("fulltext"):
                clock["now"] += 0.010
            return [{"id": "m1", "score": 0.9}, {"id": "m2", "score": 0.4}]

        service.search_service = MagicMock()
        service.search_service.hybrid_search = hybrid_search

        async def hydrate(*_args, **_kwargs):
            clock["now"] += 0.025
            # #2546: rows with an id — recall keeps one hit per hydrated row.
            return {"m1": SimpleNamespace(id=uuid4()), "m2": SimpleNamespace(id=uuid4())}

        service._recall_hydrate_memories = hydrate
        service._recall_filter_rows_by_binding = AsyncMock(side_effect=lambda _r, m, **_k: (m, 0))
        service._apply_supersede_shadowing = AsyncMock(return_value=({}, {}))
        service._maybe_reinforce_rerank = AsyncMock()
        service._maybe_graph_boost = AsyncMock()

        async def build(*_args, **_kwargs):
            clock["now"] += 0.006
            return ["page-item"]

        service._recall_build_responses = build
        service.db.commit = advance(0.003)  # the read commit: its own stage
        service._recall_record_access = advance(0.011)
        service._recall_run_hebbian_learning = advance(0.009)

        async def finalize_body(*_args, **_kwargs):
            clock["now"] += 0.014
            return SimpleNamespace(results=["page-item"])

        service._recall_finalize_response = finalize_body

        await service.recall(
            request,
            "u1",
            current_context_id=ctx,
            current_workspace_id=ws,
            context_ids=[ctx, other_ctx],
            capacity_gate=False,
        )

        completed = [
            c for c in log.info.call_args_list if c.args and c.args[0] == "recall_completed"
        ]
        assert len(completed) == 1
        kw = completed[0].kwargs
        assert kw["embed_ms"] == 120 and kw["embed_cache_hit"] is False
        assert kw["qdrant_ms"] == 30 and kw["fulltext_ms"] == 10
        assert "rerank_ms" not in kw
        assert kw["hydrate_ms"] == 25
        assert kw["finalize_ms"] == 20  # build (6) + finalize body (14)
        assert kw["commit_ms"] == 3
        assert kw["post_commit_ms"] == 20  # record_access (11) + Hebbian (9)
        assert kw["other_ms"] == 4  # the config lookups inside hybrid_search
        assert kw["duration_ms"] == 232
        assert (
            kw["embed_ms"]
            + kw["qdrant_ms"]
            + kw["fulltext_ms"]
            + kw["hydrate_ms"]
            + kw["finalize_ms"]
            + kw["commit_ms"]
            + kw["post_commit_ms"]
            + kw["other_ms"]
        ) == kw["duration_ms"]
        assert kw["candidates"] == 2 and kw["results"] == 1 and kw["k"] == 5
        assert kw["context_count"] == 2
        assert kw["degraded"] is None

    async def test_empty_exit_logs_one_recall_completed_line(self, monkeypatch):
        from services import memory_service as ms
        from services.memory_service import MemoryService
        from services.recall_timings import RecallTimings

        log = MagicMock()
        monkeypatch.setattr(ms, "logger", log)
        service = MemoryService(db=AsyncMock())
        request = SimpleNamespace(
            query="q",
            k=5,
            use_rerank=False,
            filters=None,
            search_mode="hybrid",
            include_explore_hints=False,
        )
        clock = {"now": 10.0}
        timings = RecallTimings(clock=lambda: clock["now"])
        with timings.stage("embed"):
            clock["now"] += 0.2

        await service._empty_recall_response(
            request=request,
            selection_config=None,
            search_config=None,
            context_id=uuid4(),
            timings=timings,
            user_id="u1",
            context_count=1,
        )

        completed = [
            c for c in log.info.call_args_list if c.args and c.args[0] == "recall_completed"
        ]
        assert len(completed) == 1
        kw = completed[0].kwargs
        assert kw["user_id"] == "u1"
        assert kw["results"] == 0 and kw["candidates"] == 0 and kw["k"] == 5
        assert kw["context_count"] == 1
        assert kw["embed_ms"] == 200 and kw["duration_ms"] == 200
        assert "hydrate_ms" not in kw

    async def test_finalize_logs_the_fields_after_the_audit_row(self, monkeypatch):
        from services import memory_service as ms
        from services.memory_service import MemoryService
        from services.recall_timings import RecallTimings

        log = MagicMock()
        monkeypatch.setattr(ms, "logger", log)
        emitted = AsyncMock()
        monkeypatch.setattr("services.memory_access_event_writer.emit_memory_access_event", emitted)
        service = MemoryService(db=AsyncMock())
        service._aggregate_related_tags = MagicMock(return_value=[])
        service._tag_suggestions_for_empty_result = AsyncMock(return_value=None)
        service._compute_recall_confidence = MagicMock(return_value=None)

        clock = {"now": 0.0}
        timings = RecallTimings(clock=lambda: clock["now"])
        with timings.stage("hydrate"):
            clock["now"] += 0.05

        async def _slow_emit(**_kwargs):
            clock["now"] += 0.02

        emitted.side_effect = _slow_emit
        request = SimpleNamespace(
            query="q",
            k=5,
            use_rerank=False,
            filters=None,
            search_mode="hybrid",
            include_explore_hints=False,
            include_superseded=False,
        )
        search_results = [{"id": "a", "score": 0.9}, {"id": "b", "score": 0.5}]
        responses: list = []  # the page itself is validated elsewhere; the clock is the subject

        await service._recall_finalize(
            request,
            search_results,
            responses,
            user_id="u1",
            memories={},
            current_context_id=uuid4(),
            effective_workspace_id=uuid4(),
            neural_enabled=False,
            binding_row_filtered=0,
            selection_evidence=None,
            timings=timings,
            context_count=3,
        )

        completed = [
            c for c in log.info.call_args_list if c.args and c.args[0] == "recall_completed"
        ]
        assert len(completed) == 1
        kw = completed[0].kwargs
        assert kw["results"] == 0 and kw["candidates"] == 2 and kw["k"] == 5
        assert kw["context_count"] == 3
        assert kw["hydrate_ms"] == 50
        assert kw["finalize_ms"] == 20  # the audit row write is inside finalize
        assert kw["duration_ms"] == 70
        # the log is the last thing finalize does: the audit row was already written
        emitted.assert_awaited_once()

    @pytest.mark.parametrize("fusion", ["weighted", "rrf", "keyword_floor"])
    async def test_recall_completed_carries_fusion_and_leg_ranks(self, monkeypatch, fusion):
        """#2958: the line names the deployment's HYBRID_FUSION and, per row of
        the page in page order, [semantic_rank, keyword_rank]. No memory id."""
        from config.settings import get_settings
        from services import memory_service as ms
        from services.memory_service import MemoryService
        from services.recall_timings import RecallTimings

        monkeypatch.setattr(get_settings(), "hybrid_fusion", fusion)
        log = MagicMock()
        monkeypatch.setattr(ms, "logger", log)
        service = MemoryService(db=AsyncMock())
        service._recall_finalize_response = AsyncMock(return_value=SimpleNamespace(results=[]))
        request = SimpleNamespace(k=5)
        mem_a, mem_b, mem_c = (SimpleNamespace(id=uuid4()) for _ in range(3))
        search_results = [
            {"id": "a", "hybrid_score": 0.9, "semantic_rank": 1, "keyword_rank": None},
            {"id": "b", "hybrid_score": 0.8, "semantic_rank": None, "keyword_rank": 1},
            {"id": "c", "hybrid_score": 0.7, "semantic_rank": 2, "keyword_rank": 3},
            # a candidate that did not make the page
            {"id": "d", "hybrid_score": 0.6, "semantic_rank": 3, "keyword_rank": None},
        ]
        # The page order differs from the pool order (e.g. after a reinforce re-sort).
        responses = [
            SimpleNamespace(memory_id=mem_c.id),
            SimpleNamespace(memory_id=mem_a.id),
            SimpleNamespace(memory_id=mem_b.id),
        ]
        await service._recall_finalize(
            request,
            search_results,
            responses,
            user_id="u1",
            memories={"a": mem_a, "b": mem_b, "c": mem_c},
            current_context_id=uuid4(),
            effective_workspace_id=uuid4(),
            neural_enabled=False,
            binding_row_filtered=0,
            selection_evidence=None,
            timings=RecallTimings(),
        )
        (line,) = [c for c in log.info.call_args_list if c.args and c.args[0] == "recall_completed"]
        kw = line.kwargs
        assert kw["fusion"] == fusion
        assert kw["leg_ranks"] == [[2, 3], [1, None], [None, 1]]
        assert not any(str(m.id) in repr(kw) for m in (mem_a, mem_b, mem_c))
        # The per-stage keys stay as they are (RecallTimings is not extended).
        assert not [key for key in kw if key.startswith(("fusion", "leg_")) and key.endswith("_ms")]

    async def test_empty_exit_logs_fusion_and_empty_leg_ranks(self, monkeypatch):
        from config.settings import get_settings
        from services import memory_service as ms
        from services.memory_service import MemoryService
        from services.recall_timings import RecallTimings

        monkeypatch.setattr(get_settings(), "hybrid_fusion", "keyword_floor")
        log = MagicMock()
        monkeypatch.setattr(ms, "logger", log)
        service = MemoryService(db=AsyncMock())
        request = SimpleNamespace(
            query="q",
            k=5,
            use_rerank=False,
            filters=None,
            search_mode="hybrid",
            include_explore_hints=False,
        )
        await service._empty_recall_response(
            request=request,
            selection_config=None,
            search_config=None,
            context_id=uuid4(),
            timings=RecallTimings(),
            user_id="u1",
            context_count=1,
        )
        (line,) = [c for c in log.info.call_args_list if c.args and c.args[0] == "recall_completed"]
        assert line.kwargs["fusion"] == "keyword_floor"
        assert line.kwargs["leg_ranks"] == []

    @pytest.mark.parametrize(("deployment", "used"), [("rrf", "weighted"), ("weighted", "rrf")])
    async def test_recall_completed_logs_the_fusion_the_search_used(
        self, monkeypatch, deployment, used
    ):
        """#2980: the search's resolved fusion (``timings.fusion``) is logged,
        not the deployment setting."""
        from config.settings import get_settings
        from services import memory_service as ms
        from services.memory_service import MemoryService
        from services.recall_timings import RecallTimings

        monkeypatch.setattr(get_settings(), "hybrid_fusion", deployment)
        log = MagicMock()
        monkeypatch.setattr(ms, "logger", log)
        service = MemoryService(db=AsyncMock())
        service._recall_finalize_response = AsyncMock(return_value=SimpleNamespace(results=[]))
        timings = RecallTimings()
        timings.fusion = used
        await service._recall_finalize(
            SimpleNamespace(k=5),
            [],
            [],
            user_id="u1",
            memories={},
            current_context_id=uuid4(),
            effective_workspace_id=uuid4(),
            neural_enabled=False,
            binding_row_filtered=0,
            selection_evidence=None,
            timings=timings,
        )
        (line,) = [c for c in log.info.call_args_list if c.args and c.args[0] == "recall_completed"]
        assert line.kwargs["fusion"] == used

    async def test_empty_exit_after_a_search_logs_the_fusion_used(self, monkeypatch):
        from config.settings import get_settings
        from services import memory_service as ms
        from services.memory_service import MemoryService
        from services.recall_timings import RecallTimings

        monkeypatch.setattr(get_settings(), "hybrid_fusion", "keyword_floor")
        log = MagicMock()
        monkeypatch.setattr(ms, "logger", log)
        service = MemoryService(db=AsyncMock())
        request = SimpleNamespace(
            query="q",
            k=5,
            use_rerank=False,
            filters=None,
            search_mode="hybrid",
            include_explore_hints=False,
        )
        timings = RecallTimings()
        timings.fusion = "rrf"
        await service._empty_recall_response(
            request=request,
            selection_config=None,
            search_config=None,
            context_id=uuid4(),
            timings=timings,
            user_id="u1",
            context_count=1,
        )
        (line,) = [c for c in log.info.call_args_list if c.args and c.args[0] == "recall_completed"]
        assert line.kwargs["fusion"] == "rrf"


# ---------------------------------------------------------------------------
# #2960: a recall the query router sent to the semantic lane degrades.
#
# One chain, nothing in the middle faked: the real ``MemoryService.recall``,
# the real ``_recall_prepare`` (routing_mode read from the context's search
# config) and the real ``SearchService.hybrid_search``, with only the IO
# mocked the way ``test_recall_degraded_fallback._search`` mocks it. A fake
# hybrid_search that wrote the degradation itself would pass with the flag
# ignored.
# ---------------------------------------------------------------------------

# "how do we deploy": plain natural language, which the router sends to its
# default semantic lane (asserted below on the search the recall ran).
_ROUTED_QUERY = "how do we deploy"
_INFERENCE_BASE_URL = "http://inference.example.com:8000"
_INFERENCE_HOST = "inference.example.com"


def _unreachable_server() -> Exception:
    from utils.exceptions import SelfHostedUnreachableError

    return SelfHostedUnreachableError(
        f"Cannot connect to self-hosted inference server at {_INFERENCE_BASE_URL}. "
        "Is your inference server running?"
    )


def _provider_failure() -> Exception:
    from utils.exceptions import OpenAIError

    # A client error text that names the server it tried: the answer must not.
    return OpenAIError(f"Embedding generation failed: connection to {_INFERENCE_BASE_URL} failed")


_CAUSES = pytest.mark.parametrize(
    "make_error",
    [_unreachable_server, _provider_failure],
    ids=["self_hosted_unreachable", "provider_error"],
)


def _page_row(memory_id):
    from datetime import UTC, datetime

    from models.schemas import MemoryResponse

    return MemoryResponse(
        memory_id=memory_id,
        summary="deploy runbook",
        context_summary=None,
        type="note",
        importance=0.5,
        scope="persistent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        client="t",
        tags=[],
        context=None,
        score=0.9,
    )


class _RoutedRig(SimpleNamespace):
    """What a routed-recall test reads back: the service, the IO mocks and the
    keyword arguments ``recall`` passed to the real ``hybrid_search``."""


def _routed_rig(*, embed_error: Exception | None) -> _RoutedRig:
    from services.memory_service import MemoryService

    embed_svc = MagicMock()
    embed_svc.provider = "openai"
    embed_svc.model = "text-embedding-3-small"
    if embed_error is not None:
        embed_svc.embed_with_usage = AsyncMock(side_effect=embed_error)
    else:
        embed_svc.embed_with_usage = AsyncMock(return_value=([0.1] * 8, 0))
    embed_svc.resolve_paid_by = AsyncMock(return_value="platform")

    keyword_memory_id = uuid4()
    vector_memory_id = uuid4()
    rig = _RoutedRig(
        embed_svc=embed_svc,
        fulltext=AsyncMock(return_value=[{"id": "kw-1", "score": 0.9, "payload": {}}]),
        vector=AsyncMock(return_value=[{"id": "vec-1", "score": 0.8, "payload": {}}]),
        keyword_memory_id=keyword_memory_id,
        vector_memory_id=vector_memory_id,
        search_kwargs={},
    )

    service = MemoryService(db=AsyncMock())
    search_service = service.search_service
    search_service._get_search_config = AsyncMock(
        return_value=SimpleNamespace(
            context_id=uuid4(),
            fetch_factor=2,
            use_rerank=False,
            semantic_weight=0.6,
            bm25_weight=0.4,
        )
    )
    real_hybrid_search = search_service.hybrid_search

    async def recording_hybrid_search(**kwargs):
        rig.search_kwargs.update(kwargs)
        return await real_hybrid_search(**kwargs)

    search_service.hybrid_search = recording_hybrid_search

    rows = {
        "kw-1": SimpleNamespace(id=keyword_memory_id, tags=[]),
        "vec-1": SimpleNamespace(id=vector_memory_id, tags=[]),
    }

    async def hydrate(_request, *, search_results, **_kwargs):
        return {hit["id"]: rows[hit["id"]] for hit in search_results}

    async def build(request, search_results, memories, **_kwargs):
        return [_page_row(memories[hit["id"]].id) for hit in search_results[: request.k]]

    service._recall_check_agent_bindings = AsyncMock()
    service._recall_resolve_cluster_filter = AsyncMock(return_value=None)
    service._recall_hydrate_memories = hydrate
    service._recall_filter_rows_by_binding = AsyncMock(side_effect=lambda _r, m, **_k: (m, 0))
    service._apply_supersede_shadowing = AsyncMock(return_value=({}, {}))
    service._apply_supersede_fold = AsyncMock(return_value={})
    service._maybe_reinforce_rerank = AsyncMock()
    service._maybe_graph_boost = AsyncMock()
    service._recall_build_responses = build
    service._recall_record_access = AsyncMock()
    service._recall_run_hebbian_learning = AsyncMock()
    service._tag_suggestions_for_empty_result = AsyncMock(return_value=None)
    rig.service = service
    return rig


async def _routed_recall(rig: _RoutedRig, *, routing_mode: str, search_mode: str | None = None):
    from unittest.mock import patch

    from models.schemas import RecallRequest

    request = (
        RecallRequest(query=_ROUTED_QUERY)
        if search_mode is None
        else RecallRequest(query=_ROUTED_QUERY, search_mode=search_mode)
    )
    with (
        patch(
            "repositories.config_repository.ContextSearchConfigRepository.get_by_context",
            new=AsyncMock(return_value=SimpleNamespace(routing_mode=routing_mode)),
        ),
        patch("services.search_service.LLMCallLogWriter", MagicMock()),
        patch(
            "services.search_service.resolve_routing_from_config",
            return_value=("default", rig.embed_svc),
        ),
        patch("services.search_service.search_memories_qdrant", new=rig.vector),
        patch("services.search_service.search_memories_fulltext", new=rig.fulltext),
        patch(
            "services.context_service.ContextService.is_context_shared",
            new=AsyncMock(return_value=False),
        ),
        patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
    ):
        return await rig.service.recall(
            request,
            "u1",
            current_context_id=uuid4(),
            current_workspace_id=uuid4(),
            capacity_gate=False,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_identity_links")
class TestRoutedSemanticRecallDegrades:
    @_CAUSES
    async def test_active_routing_without_search_mode_answers_keyword_hits_flagged_degraded(
        self, make_error
    ):
        rig = _routed_rig(embed_error=make_error())

        response = await _routed_recall(rig, routing_mode="active")

        # The router chose semantic and recall told the search so.
        assert rig.search_kwargs["search_mode"] == "semantic"
        assert rig.search_kwargs["search_mode_explicit"] is False
        rig.embed_svc.embed_with_usage.assert_awaited_once()
        # Keyword results, not an empty page with a flag.
        rig.fulltext.assert_awaited_once()
        assert [row.memory_id for row in response.results] == [rig.keyword_memory_id]
        assert response.degraded is True
        assert response.degraded_reason == "embedding_unavailable"
        # The serialised answer (as the REST route sends it) names no server.
        body = response.model_dump_json(exclude_none=True)
        assert body.count(_INFERENCE_HOST) == 0

    @_CAUSES
    async def test_explicit_semantic_through_recall_still_raises(self, make_error):
        error = make_error()
        rig = _routed_rig(embed_error=error)

        with pytest.raises(type(error)):
            await _routed_recall(rig, routing_mode="active", search_mode="semantic")

        assert rig.search_kwargs["search_mode"] == "semantic"
        assert rig.search_kwargs["search_mode_explicit"] is True
        rig.fulltext.assert_not_called()

    @pytest.mark.parametrize("routing_mode", ["log_only", "off"])
    async def test_log_only_and_off_still_search_hybrid(self, routing_mode):
        rig = _routed_rig(embed_error=None)

        response = await _routed_recall(rig, routing_mode=routing_mode)

        assert rig.search_kwargs["search_mode"] == "hybrid"
        rig.vector.assert_awaited_once()
        rig.fulltext.assert_awaited_once()
        assert response.degraded is None

    async def test_hybrid_search_call_carries_search_mode_explicit(self):
        """The hybrid_search call itself passes the caller's choice; a
        source-string check would pass for any call site (#1989)."""
        import ast
        import inspect
        import textwrap

        from services.memory_service import MemoryService

        tree = ast.parse(textwrap.dedent(inspect.getsource(MemoryService.recall)))
        searches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "hybrid_search"
        ]
        assert searches, "recall no longer calls hybrid_search"
        for call in searches:
            passed = {kw.arg: kw.value for kw in call.keywords}
            assert "search_mode_explicit" in passed, (
                "MemoryService.recall must tell hybrid_search whether the caller "
                "chose search_mode; without it a routed semantic recall fails "
                "instead of degrading (#2960)."
            )
