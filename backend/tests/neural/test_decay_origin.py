"""DecayManager passes only_origin='hebbian' to repository (Issue #722)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from models.memory import EDGE_ORIGIN_HEBBIAN
from neural.decay import DecayManager


@pytest.mark.asyncio
async def test_apply_decay_passes_only_origin_hebbian():
    edge_repo = MagicMock()
    edge_repo.bulk_decay_weights = AsyncMock(return_value=5)
    edge_repo.prune_weak_edges = AsyncMock(return_value=1)
    graph = MagicMock(edge_repo=edge_repo)
    config = MagicMock(
        decay_rate=0.001,
        prune_threshold=0.01,
        decay_background_interval=3600,
    )
    mgr = DecayManager(graph=graph, config=config)

    await mgr.apply_decay("user1")

    # Tight assertions: catch user_id drift AND missing kwarg in one place.
    bd_call = edge_repo.bulk_decay_weights.await_args
    assert bd_call.args[0] == "user1"
    assert bd_call.kwargs.get("only_origin") == EDGE_ORIGIN_HEBBIAN

    pw_call = edge_repo.prune_weak_edges.await_args
    assert pw_call.args[0] == "user1"
    assert pw_call.kwargs.get("only_origin") == EDGE_ORIGIN_HEBBIAN


@pytest.mark.asyncio
async def test_standalone_prune_weak_edges_passes_only_origin_hebbian():
    edge_repo = MagicMock()
    edge_repo.prune_weak_edges = AsyncMock(return_value=0)
    graph = MagicMock(edge_repo=edge_repo)
    config = MagicMock(prune_threshold=0.01)
    mgr = DecayManager(graph=graph, config=config)

    await mgr.prune_weak_edges("user1")

    edge_repo.prune_weak_edges.assert_awaited_once()
    assert edge_repo.prune_weak_edges.await_args.kwargs.get("only_origin") == EDGE_ORIGIN_HEBBIAN
