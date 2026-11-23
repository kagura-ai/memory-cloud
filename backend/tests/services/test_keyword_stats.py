"""#2984: the keyword leg's per-context statistics (``services.keyword_stats``)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from services import keyword_stats
from services.keyword_stats import (
    LENGTH_COLUMNS,
    STATS_TTL_SECONDS,
    apply_keyword_lengths,
    memory_field_stats,
    memory_keyword_lengths,
    resource_fulltext_avgdl,
    resource_keyword_lengths,
)
from utils.sparse_vector import MemoryFieldStats


def _session(*, one=None, scalar=None) -> MagicMock:
    db = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.one.return_value = one
    db.execute = AsyncMock(return_value=result)
    db.scalar = AsyncMock(return_value=scalar)
    return db


def _sql(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


@pytest.fixture(autouse=True)
def _empty_cache():
    keyword_stats.clear_keyword_stats_cache()
    yield
    keyword_stats.clear_keyword_stats_cache()


def test_ttl_is_five_minutes():
    assert STATS_TTL_SECONDS == 300.0


@pytest.mark.asyncio
async def test_memory_averages_over_live_rows_with_the_field_indexed():
    context = uuid4()
    db = _session(one=(4.5, None, 120.0, 7.25))

    stats = await memory_field_stats(db, context)

    assert stats == MemoryFieldStats(
        summary=4.5, context_summary=None, content=120.0, summary_reading=7.25
    )
    sql = _sql(db.execute.await_args.args[0])
    assert f"memories.context_id = '{context}'" in sql
    assert "memories.deleted_at IS NULL" in sql
    assert "memories.embedding_suppressed_at IS NULL" in sql
    # NULL (never indexed since the upgrade) and 0 (empty field) are left out.
    for column in (
        "keyword_summary_len",
        "keyword_context_summary_len",
        "keyword_content_len",
        "keyword_reading_len",
    ):
        assert f"avg(memories.{column}) FILTER (WHERE memories.{column} > 0)" in sql
    assert "keyword_fulltext_len" not in sql


@pytest.mark.asyncio
async def test_resource_average_is_its_own_column():
    context = uuid4()
    db = _session(scalar=900.0)

    assert await resource_fulltext_avgdl(db, context) == 900.0

    sql = _sql(db.scalar.await_args.args[0])
    assert (
        "avg(memories.keyword_fulltext_len) FILTER (WHERE memories.keyword_fulltext_len > 0)" in sql
    )
    assert "memories.deleted_at IS NULL" in sql
    assert "memories.embedding_suppressed_at IS NULL" in sql
    assert "keyword_summary_len" not in sql


@pytest.mark.asyncio
async def test_no_qualifying_rows_reads_none():
    db = _session(one=(None, None, None, None), scalar=None)
    assert await memory_field_stats(db, uuid4()) == MemoryFieldStats()
    assert await resource_fulltext_avgdl(db, uuid4()) is None


@pytest.mark.asyncio
async def test_a_context_less_row_has_no_averages_and_reads_nothing():
    db = _session()
    assert await memory_field_stats(db, None) == MemoryFieldStats()
    assert await resource_fulltext_avgdl(db, None) is None
    db.execute.assert_not_awaited()
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_per_context_and_kind_until_the_ttl(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(keyword_stats.time, "monotonic", lambda: clock[0])
    context = uuid4()
    db = _session(one=(3.0, 3.0, 3.0, 3.0), scalar=50.0)

    await memory_field_stats(db, context)
    await memory_field_stats(db, context)
    await resource_fulltext_avgdl(db, context)
    await resource_fulltext_avgdl(db, context)
    assert db.execute.await_count == 1
    assert db.scalar.await_count == 1

    # Another context is read on its own.
    await memory_field_stats(db, uuid4())
    assert db.execute.await_count == 2

    clock[0] += STATS_TTL_SECONDS - 1
    await memory_field_stats(db, context)
    assert db.execute.await_count == 2

    clock[0] += 2
    await memory_field_stats(db, context)
    await resource_fulltext_avgdl(db, context)
    assert db.execute.await_count == 3
    assert db.scalar.await_count == 2


@pytest.mark.asyncio
async def test_fresh_reads_past_the_cache_and_refreshes_it():
    context = uuid4()
    db = _session(one=(3.0, None, None, None))
    await memory_field_stats(db, context)
    db.execute.return_value.one.return_value = (9.0, None, None, None)

    assert (await memory_field_stats(db, context, fresh=True)).summary == 9.0
    assert (await memory_field_stats(db, context)).summary == 9.0
    assert db.execute.await_count == 2


def test_memory_lengths_are_counted_from_the_payload_token_fields():
    payload = {
        "summary_tokens": "a b c",
        "context_summary_tokens": "",
        "content_tokens": "x " * 7,
        "summary_reading": "r r",
    }
    assert memory_keyword_lengths(payload) == {
        "keyword_summary_len": 3,
        "keyword_context_summary_len": 0,
        "keyword_content_len": 7,
        "keyword_reading_len": 2,
        "keyword_fulltext_len": None,
    }


def test_resource_lengths_fill_only_the_fulltext_column():
    assert resource_keyword_lengths("d e f g") == {
        "keyword_summary_len": None,
        "keyword_context_summary_len": None,
        "keyword_content_len": None,
        "keyword_reading_len": None,
        "keyword_fulltext_len": 4,
    }
    assert set(resource_keyword_lengths("")) == set(LENGTH_COLUMNS)


def test_apply_reports_whether_a_length_changed():
    row = MagicMock()
    for column in LENGTH_COLUMNS:
        setattr(row, column, None)
    lengths = resource_keyword_lengths("a b")
    assert apply_keyword_lengths(row, lengths) is True
    assert row.keyword_fulltext_len == 2
    assert apply_keyword_lengths(row, lengths) is False


@pytest.mark.asyncio
async def test_the_lancedb_store_reads_no_averages(monkeypatch):
    """LanceDB keeps no sparse vector (its own FTS scores with BM25): the
    writers there get no averages and nothing is read."""
    import db.qdrant as qdrant

    monkeypatch.setattr(qdrant, "_active_store", lambda: object())
    db = _session(one=(4.5, 1.0, 120.0, 7.25), scalar=300.0)

    assert await memory_field_stats(db, uuid4()) == MemoryFieldStats()
    assert await resource_fulltext_avgdl(db, uuid4()) is None
    db.execute.assert_not_awaited()
    db.scalar.assert_not_awaited()
