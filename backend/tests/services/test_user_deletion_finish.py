"""``finish_user_deletion`` after the deletion commits (#2406).

The deletion has committed by then, so nothing it does may raise: a vector
store that fails to list its collections or to delete a context's points is
logged, every other context is still tried, and ``sweep_orphan_vectors``
removes what is left.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call, patch
from uuid import uuid4

import pytest

from services.user_deletion_service import UserDeletionResult, finish_user_deletion
from utils.exceptions import QdrantError


@pytest.mark.asyncio
async def test_a_failing_vector_store_never_raises_and_every_context_is_tried():
    ws = uuid4()
    first, second = uuid4(), uuid4()
    result = UserDeletionResult(deleted_contexts=[(ws, first), (ws, second)])
    points = AsyncMock(side_effect=[QdrantError("down"), {}])

    with (
        patch("db.qdrant.list_memory_collections", AsyncMock(side_effect=QdrantError("down"))),
        patch("db.qdrant.delete_context_points_everywhere", points),
    ):
        await finish_user_deletion(result)

    assert points.await_args_list == [
        call(str(ws), str(first), collections=None),
        call(str(ws), str(second), collections=None),
    ]


@pytest.mark.asyncio
async def test_collections_are_listed_once_for_all_contexts():
    ws = uuid4()
    contexts = [(ws, uuid4()), (ws, uuid4())]
    listing = AsyncMock(return_value=["kagura_memories"])
    points = AsyncMock(return_value={})

    with (
        patch("db.qdrant.list_memory_collections", listing),
        patch("db.qdrant.delete_context_points_everywhere", points),
    ):
        await finish_user_deletion(UserDeletionResult(deleted_contexts=contexts))

    listing.assert_awaited_once()
    assert [c.kwargs["collections"] for c in points.await_args_list] == [["kagura_memories"]] * 2


@pytest.mark.asyncio
async def test_nothing_deleted_touches_no_vector_store():
    listing = AsyncMock()
    with patch("db.qdrant.list_memory_collections", listing):
        await finish_user_deletion(UserDeletionResult())
    listing.assert_not_awaited()
