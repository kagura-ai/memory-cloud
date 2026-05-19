"""Sleep edge_discovery writes origin='semantic' with cosine weight (Issue #722)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from models.memory import EDGE_ORIGIN_SEMANTIC
from services.sleep.edge_discovery import DISCOVERY_EDGE_WEIGHT, ConfirmedEdge, EdgeDiscoveryPhase


def _make_phase():
    """Build an EdgeDiscoveryPhase with all external deps mocked out."""
    db = AsyncMock()
    llm_service = MagicMock()
    with (
        patch("services.sleep.edge_discovery.NeuralEdgeRepository"),
        patch("services.sleep.edge_discovery.EmbeddingService"),
    ):
        phase = EdgeDiscoveryPhase(db, llm_service)
        phase.edge_repo = MagicMock()
        phase.edge_repo.create_or_update_edge = AsyncMock(return_value=MagicMock())
    return phase


@pytest.mark.asyncio
async def test_persist_confirmed_edges_uses_origin_semantic_and_cosine_weight():
    """Each persisted sleep edge must carry origin='semantic' and weight = batch cosine."""
    phase = _make_phase()

    src1, dst1 = uuid4(), uuid4()
    src2, dst2 = uuid4(), uuid4()
    batch = [(src1, dst1, 0.82), (src2, dst2, 0.71)]
    confirmed = [
        ConfirmedEdge(src_id=s, dst_id=d, edge_type="related_to", confidence=0.9)
        for s, d, _ in batch
    ]

    created = await phase._persist_confirmed_edges(
        confirmed=confirmed,
        batch=batch,
        user_id="u",
        workspace_id="w",
        context_id="c",
        reporter=None,
        report_id=None,
    )

    assert created == 2
    calls = phase.edge_repo.create_or_update_edge.await_args_list
    assert len(calls) == 2

    kwargs_by_src = {c.kwargs["src_id"]: c.kwargs for c in calls}
    assert kwargs_by_src[src1]["weight"] == pytest.approx(0.82)
    assert kwargs_by_src[src1]["origin"] == EDGE_ORIGIN_SEMANTIC
    assert kwargs_by_src[src2]["weight"] == pytest.approx(0.71)
    assert kwargs_by_src[src2]["origin"] == EDGE_ORIGIN_SEMANTIC


@pytest.mark.asyncio
async def test_persist_falls_back_when_batch_lacks_score():
    """If a confirmed edge's (src,dst) is not in the batch score map, use a sensible default."""
    phase = _make_phase()

    src, dst = uuid4(), uuid4()
    batch: list[tuple[UUID, UUID, float]] = []  # empty score map
    confirmed = [ConfirmedEdge(src_id=src, dst_id=dst, edge_type="related_to", confidence=0.7)]

    await phase._persist_confirmed_edges(
        confirmed=confirmed,
        batch=batch,
        user_id="u",
        workspace_id="w",
        context_id="c",
        reporter=None,
        report_id=None,
    )
    kwargs = phase.edge_repo.create_or_update_edge.await_args.kwargs
    assert kwargs["origin"] == EDGE_ORIGIN_SEMANTIC
    # Default weight when no score is known — DISCOVERY_EDGE_WEIGHT (0.5) is the fallback.
    assert kwargs["weight"] == pytest.approx(DISCOVERY_EDGE_WEIGHT)


@pytest.mark.asyncio
async def test_persist_records_origin_in_reporter_details():
    """Reporter audit log captures origin='semantic' for each persisted edge."""
    phase = _make_phase()
    reporter = MagicMock()
    reporter.add_action = AsyncMock()

    src, dst = uuid4(), uuid4()
    batch: list[tuple[UUID, UUID, float]] = [(src, dst, 0.77)]
    confirmed = [ConfirmedEdge(src_id=src, dst_id=dst, edge_type="related_to", confidence=0.9)]

    await phase._persist_confirmed_edges(
        confirmed=confirmed,
        batch=batch,
        user_id="u",
        workspace_id="w",
        context_id="c",
        reporter=reporter,
        report_id="report-1",
    )

    reporter.add_action.assert_awaited_once()
    kwargs = reporter.add_action.await_args.kwargs
    assert kwargs["details"]["origin"] == EDGE_ORIGIN_SEMANTIC
    assert kwargs["details"]["weight"] == pytest.approx(0.77)
