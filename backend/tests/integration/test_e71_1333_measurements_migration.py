"""Integration pins for #1333 — the ``measurements`` table at the DB level.

Covers what the mocked-session unit tests cannot: the PostgreSQL defaults
(``gen_random_uuid()`` id, ``now()`` created_at), the NOT NULL constraints,
the FK CASCADE on context delete, and the series-scan index
``idx_measurements_context_metric_measured_at`` — all against the schema at
alembic head (mirrors tests/integration/test_e44_1065_host_arbitration_migration.py).
Runs under ``make test-integration`` (needs the kagura_test DB).
"""

import datetime
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Static SQL (no f-strings — project rule forbids interpolated SQL).
_INSERT_MINIMAL = (
    "INSERT INTO measurements (context_id, metric, measured_at, value) "
    "VALUES (:ctx, :metric, now(), :val) RETURNING id, created_at"
)
_INSERT_FULL = (
    "INSERT INTO measurements (context_id, metric, measured_at, value, unit, details) "
    "VALUES (:ctx, :metric, :at, :val, :unit, CAST(:details AS jsonb))"
)
_SELECT_BY_CTX = (
    "SELECT metric, value, unit FROM measurements WHERE context_id = :ctx ORDER BY measured_at"
)
_COUNT_BY_CTX = "SELECT COUNT(*) FROM measurements WHERE context_id = :ctx"
_DELETE_CONTEXT = "DELETE FROM contexts WHERE id = :ctx"
_SELECT_INDEXES = "SELECT indexname FROM pg_indexes WHERE tablename = 'measurements'"
_INSERT_NULL_VALUE = (
    "INSERT INTO measurements (context_id, metric, measured_at) VALUES (:ctx, :metric, now())"
)


async def _ctx(db_session):
    """Create a workspace + context and return the context id."""
    ws, ctx, owner = uuid.uuid4(), uuid.uuid4(), "u-1333"
    await db_session.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :n, :o)"),
        {"id": ws, "n": "ws-1333", "o": owner},
    )
    await db_session.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) VALUES (:id, :ws, :n, :by)"
        ),
        {"id": ctx, "ws": ws, "n": "ctx-1333", "by": owner},
    )
    return ctx


@pytest.mark.asyncio
async def test_defaults_populate_id_and_created_at(db_session):
    """id (gen_random_uuid) and created_at (now()) backfill server-side."""
    ctx = await _ctx(db_session)
    row = (
        await db_session.execute(
            text(_INSERT_MINIMAL), {"ctx": ctx, "metric": "weight_kg", "val": 72.5}
        )
    ).one()
    assert isinstance(row.id, uuid.UUID)
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_full_insert_round_trips(db_session):
    ctx = await _ctx(db_session)
    await db_session.execute(
        text(_INSERT_FULL),
        {
            "ctx": ctx,
            "metric": "weight_kg",
            "at": datetime.datetime(2026, 7, 17, 8, 0, 0),  # noqa: DTZ001 — naive UTC by convention
            "val": 72.5,
            "unit": "kg",
            "details": '{"device": "scale"}',
        },
    )
    rows = (await db_session.execute(text(_SELECT_BY_CTX), {"ctx": ctx})).all()
    assert len(rows) == 1
    assert rows[0].metric == "weight_kg"
    assert float(rows[0].value) == 72.5
    assert rows[0].unit == "kg"


@pytest.mark.asyncio
async def test_value_is_not_nullable(db_session):
    ctx = await _ctx(db_session)
    with pytest.raises(IntegrityError):
        await db_session.execute(text(_INSERT_NULL_VALUE), {"ctx": ctx, "metric": "weight_kg"})


@pytest.mark.asyncio
async def test_context_delete_cascades(db_session):
    """The lane must never orphan rows — measurements die with their context."""
    ctx = await _ctx(db_session)
    await db_session.execute(
        text(_INSERT_MINIMAL), {"ctx": ctx, "metric": "weight_kg", "val": 72.5}
    )
    await db_session.execute(text(_DELETE_CONTEXT), {"ctx": ctx})
    count = (await db_session.execute(text(_COUNT_BY_CTX), {"ctx": ctx})).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_series_scan_index_exists(db_session):
    """The (context_id, metric, measured_at) index backs recall_series."""
    names = {r.indexname for r in (await db_session.execute(text(_SELECT_INDEXES))).all()}
    assert "idx_measurements_context_metric_measured_at" in names
