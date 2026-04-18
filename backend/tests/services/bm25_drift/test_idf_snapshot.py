"""Unit tests for the Qdrant IDF snapshot accumulator (issue #343)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.bm25_drift.idf_snapshot import build_idf_snapshot


def _point(point_id: int, indices: list[int], *, resource: bool) -> SimpleNamespace:
    """Build a minimal Qdrant point stand-in."""
    payload = {"resource_id": "r1"} if resource else {}
    sparse = SimpleNamespace(indices=indices, values=[1.0] * len(indices))
    return SimpleNamespace(id=point_id, payload=payload, vector={"bm25": sparse})


@pytest.mark.asyncio
async def test_resource_id_payload_classifies_source() -> None:
    points = [
        _point(1, [10, 20, 30], resource=False),
        _point(2, [20, 40], resource=False),
        _point(3, [10, 50], resource=True),
        _point(4, [60], resource=True),
    ]
    client = MagicMock()
    client.scroll = AsyncMock(return_value=(points, None))

    snapshot = await build_idf_snapshot(client, "kagura_memories", uuid4())

    assert snapshot.m_memory == 2
    assert snapshot.r_resource == 2
    assert snapshot.n_global == 4
    # df_memory only counts memory-source documents.
    assert snapshot.df_memory == {10: 1, 20: 2, 30: 1, 40: 1}
    # df_global counts every document (memory + resource).
    assert snapshot.df_global == {10: 2, 20: 2, 30: 1, 40: 1, 50: 1, 60: 1}


@pytest.mark.asyncio
async def test_paginates_until_next_offset_is_none() -> None:
    page1 = [_point(1, [10], resource=False)]
    page2 = [_point(2, [20], resource=False)]
    page3 = [_point(3, [30], resource=True)]
    client = MagicMock()
    client.scroll = AsyncMock(
        side_effect=[
            (page1, "cursor-1"),
            (page2, "cursor-2"),
            (page3, None),
        ]
    )

    snapshot = await build_idf_snapshot(client, "kagura_memories", uuid4())

    # Three pages consumed, three pages of points accumulated.
    assert client.scroll.await_count == 3
    assert snapshot.m_memory == 2
    assert snapshot.r_resource == 1
    assert set(snapshot.df_global.keys()) == {10, 20, 30}


@pytest.mark.asyncio
async def test_skips_points_with_missing_or_empty_bm25_vector() -> None:
    points = [
        _point(1, [10], resource=False),
        SimpleNamespace(id=2, payload={}, vector=None),  # no vector at all
        SimpleNamespace(id=3, payload={}, vector={}),  # vector dict, no bm25 key
        SimpleNamespace(id=4, payload={}, vector={"bm25": SimpleNamespace(indices=[], values=[])}),
        _point(5, [20], resource=False),
    ]
    client = MagicMock()
    client.scroll = AsyncMock(return_value=(points, None))

    snapshot = await build_idf_snapshot(client, "kagura_memories", uuid4())

    # All five points still count toward source totals (we only skip the
    # df increment, not the document count). This matches the intent —
    # legacy rows that predate #335 are still part of the corpus, they
    # just contribute zero terms.
    assert snapshot.m_memory == 5
    assert snapshot.r_resource == 0
    assert snapshot.df_memory == {10: 1, 20: 1}


@pytest.mark.asyncio
async def test_duplicate_indices_in_one_point_only_count_once() -> None:
    # df is a count of DOCUMENTS containing a term, not term occurrences.
    points = [
        _point(1, [10, 10, 10, 20], resource=False),
        _point(2, [20, 20], resource=False),
    ]
    client = MagicMock()
    client.scroll = AsyncMock(return_value=(points, None))

    snapshot = await build_idf_snapshot(client, "kagura_memories", uuid4())

    assert snapshot.df_memory == {10: 1, 20: 2}
