"""Recall ``analysis_cluster`` filter integration test (Issue #496).

Verifies the new filter key in ``services/memory_service.py:recall``:

    recall(filters={"analysis_cluster": {"run_id": "...", "cluster_index": 3}})

Behavior under test:

- ``analysis_cluster`` pre-resolves the cluster's memory_ids via
  ``query_service.get_memory_ids_in_cluster``. Empty cluster → early
  empty return without calling ``hybrid_search`` (verified by mock
  call count).
- Non-empty cluster → ``hybrid_search`` is called with an expanded
  ``candidates_k`` (so the post-filter does not starve a small ``k``)
  and the PG SELECT step adds a ``Memory.id IN (cluster_member_ids)``
  predicate. The hybrid_search ranking still applies, but only over
  cluster members.
- Invalid filter shape → ``ValueError`` (mapped to 422 at the API layer
  by the existing validation middleware).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from models.schemas import RecallRequest


@pytest.fixture
def fake_run_id() -> UUID:
    return uuid4()


@pytest.fixture
def cluster_member_ids() -> list[UUID]:
    return [uuid4() for _ in range(5)]


def _make_service():
    """Return a MemoryService instance with mocked dependencies for unit tests."""
    from services.memory_service import MemoryService

    db = AsyncMock()
    db.execute = AsyncMock()
    svc = MemoryService(db)
    svc.search_service = MagicMock()
    svc.search_service.hybrid_search = AsyncMock(return_value=[])
    svc.memory_repo = MagicMock()
    return svc, db


@pytest.mark.asyncio
async def test_analysis_cluster_filter_short_circuits_when_cluster_empty(fake_run_id):
    """Empty cluster → return early, do not call hybrid_search."""
    svc, _db = _make_service()
    request = RecallRequest(
        query="test",
        k=5,
        filters={"analysis_cluster": {"run_id": str(fake_run_id), "cluster_index": 3}},
    )

    with patch(
        "services.analysis.query_service.get_memory_ids_in_cluster",
        AsyncMock(return_value=[]),
    ):
        response = await svc.recall(
            request=request,
            user_id="test_user",
            current_context_id=uuid4(),
            current_workspace_id=uuid4(),
        )

    assert response.results == []
    svc.search_service.hybrid_search.assert_not_called()


@pytest.mark.asyncio
async def test_analysis_cluster_filter_unknown_cluster_returns_empty(fake_run_id):
    """Unknown cluster (None from query_service) → also early empty return."""
    svc, _db = _make_service()
    request = RecallRequest(
        query="test",
        k=5,
        filters={"analysis_cluster": {"run_id": str(fake_run_id), "cluster_index": 999}},
    )

    with patch(
        "services.analysis.query_service.get_memory_ids_in_cluster",
        AsyncMock(return_value=None),
    ):
        response = await svc.recall(
            request=request,
            user_id="test_user",
            current_context_id=uuid4(),
            current_workspace_id=uuid4(),
        )

    assert response.results == []
    svc.search_service.hybrid_search.assert_not_called()


@pytest.mark.asyncio
async def test_analysis_cluster_filter_expands_candidates_k(fake_run_id, cluster_member_ids):
    """Non-empty cluster → candidates_k bumped to cover whole cluster + buffer."""
    svc, _db = _make_service()
    request = RecallRequest(
        query="test",
        k=5,
        filters={"analysis_cluster": {"run_id": str(fake_run_id), "cluster_index": 3}},
    )

    with patch(
        "services.analysis.query_service.get_memory_ids_in_cluster",
        AsyncMock(return_value=cluster_member_ids),
    ):
        await svc.recall(
            request=request,
            user_id="test_user",
            current_context_id=uuid4(),
            current_workspace_id=uuid4(),
        )

    # candidates_k = max(k or k*4, len(cluster) + 50). With 5 cluster members
    # the formula gives max(5, 55) = 55. The kwargs of hybrid_search expose ``k``.
    svc.search_service.hybrid_search.assert_called_once()
    call_kwargs = svc.search_service.hybrid_search.call_args.kwargs
    assert call_kwargs["k"] >= len(cluster_member_ids) + 50


@pytest.mark.asyncio
async def test_analysis_cluster_filter_invalid_run_id_raises():
    """``run_id`` not a UUID → ValueError."""
    svc, _db = _make_service()
    request = RecallRequest(
        query="test",
        k=5,
        filters={"analysis_cluster": {"run_id": "not-a-uuid", "cluster_index": 3}},
    )

    with pytest.raises(ValueError, match="run_id"):
        await svc.recall(
            request=request,
            user_id="test_user",
            current_context_id=uuid4(),
            current_workspace_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_analysis_cluster_filter_missing_cluster_index_raises(fake_run_id):
    """``cluster_index`` missing → ValueError."""
    svc, _db = _make_service()
    request = RecallRequest(
        query="test",
        k=5,
        filters={"analysis_cluster": {"run_id": str(fake_run_id)}},
    )

    with pytest.raises(ValueError, match="cluster_index"):
        await svc.recall(
            request=request,
            user_id="test_user",
            current_context_id=uuid4(),
            current_workspace_id=uuid4(),
        )
