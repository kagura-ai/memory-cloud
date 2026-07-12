"""Unit tests for RouterCalibrationRepository (#1220 stage 4, mock-only).

Pins the store's contract without a DB: the upsert targets the
partial-unique index matching the scope (global vs per-context — Postgres
unique constraints treat NULLs as distinct, so each scope has its own
predicate index), and reads never mix scopes (a context's own rows win;
the fleet defaults are the fallback, not a merge source).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from repositories.config_repository import RouterCalibrationRepository

_SAMPLED = datetime(2026, 7, 12, tzinfo=UTC)


def _repo() -> tuple[RouterCalibrationRepository, AsyncMock]:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return RouterCalibrationRepository(db), db


def _compiled(db: AsyncMock) -> str:
    from sqlalchemy.dialects import postgresql

    stmt = db.execute.await_args.args[0]
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class TestUpsertScopeTargeting:
    @pytest.mark.asyncio
    async def test_global_scope_targets_null_partial_index(self) -> None:
        repo, db = _repo()
        await repo.upsert(
            bucket="keyword",
            arm="routed",
            p_at_5=0.4,
            mrr_at_10=0.6,
            n_queries=30,
            sampled_at=_SAMPLED,
            context_id=None,
        )
        sql = _compiled(db)
        assert "ON CONFLICT (bucket, arm, source)" in sql
        assert "context_id IS NULL" in sql

    @pytest.mark.asyncio
    async def test_context_scope_targets_nonnull_partial_index(self) -> None:
        repo, db = _repo()
        await repo.upsert(
            bucket="semantic",
            arm="semantic",
            p_at_5=0.5,
            mrr_at_10=0.7,
            n_queries=40,
            sampled_at=_SAMPLED,
            context_id=uuid4(),
            source="live_traffic",
        )
        sql = _compiled(db)
        assert "ON CONFLICT (context_id, bucket, arm, source)" in sql
        assert "context_id IS NOT NULL" in sql

    @pytest.mark.asyncio
    async def test_conflict_refreshes_measurement_fields(self) -> None:
        repo, db = _repo()
        await repo.upsert(
            bucket="hybrid",
            arm="hybrid",
            p_at_5=0.3,
            mrr_at_10=0.5,
            n_queries=20,
            sampled_at=_SAMPLED,
        )
        sql = _compiled(db)
        assert "DO UPDATE SET" in sql
        for field in ("p_at_5", "mrr_at_10", "n_queries", "sampled_at"):
            assert field in sql.split("DO UPDATE SET")[1]


class TestGetForContext:
    @pytest.mark.asyncio
    async def test_context_rows_win_without_mixing(self) -> None:
        repo, db = _repo()
        own_rows = [MagicMock(), MagicMock()]
        result = MagicMock()
        result.scalars.return_value.all.return_value = own_rows
        db.execute = AsyncMock(return_value=result)

        rows = await repo.get_for_context(uuid4())

        assert rows == own_rows
        assert db.execute.await_count == 1  # no second (global) query

    @pytest.mark.asyncio
    async def test_falls_back_to_fleet_defaults(self) -> None:
        repo, db = _repo()
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        globals_result = MagicMock()
        fleet_rows = [MagicMock()]
        globals_result.scalars.return_value.all.return_value = fleet_rows
        db.execute = AsyncMock(side_effect=[empty, globals_result])

        rows = await repo.get_for_context(uuid4())

        assert rows == fleet_rows
        assert db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_none_context_reads_fleet_defaults_directly(self) -> None:
        repo, db = _repo()
        globals_result = MagicMock()
        fleet_rows = [MagicMock()]
        globals_result.scalars.return_value.all.return_value = fleet_rows
        db.execute = AsyncMock(return_value=globals_result)

        rows = await repo.get_for_context(None)

        assert rows == fleet_rows
        assert db.execute.await_count == 1
