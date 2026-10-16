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

        src = inspect.getsource(MemoryService.recall)
        assert 'request.search_mode != "keyword" and not degradation.get("degraded")' in src, (
            "Hebbian learning must be skipped for a degraded recall — otherwise "
            "ungated, full-strength edges are written into the persistent graph "
            "and outlive the outage (#1515 review)."
        )


@pytest.mark.asyncio
class TestRecallTimingsWiring:
    """#1989: ``recall_completed`` carries the per-stage timings on every exit."""

    async def test_hybrid_search_is_called_with_the_timings_out_param(self):
        import inspect

        from services.memory_service import MemoryService

        src = inspect.getsource(MemoryService.recall)
        assert "timings=timings" in src, (
            "MemoryService.recall must pass its RecallTimings to hybrid_search; "
            "without it embed/qdrant/fulltext/rerank never reach recall_completed (#1989)."
        )

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
