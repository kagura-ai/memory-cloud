"""DB contract and round trip for f31_2724_event_dates.

The generated columns: a ``YYYY-MM-DD`` value is copied, anything else is
NULL and never an INSERT error (on both the ``json`` and the ``jsonb`` insert
path), and they follow a details update. The partial index exists with its
predicate, is built CONCURRENTLY over existing rows and left VALID, a rerun
over an INVALID leftover rebuilds it, and downgrade drops both.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# The B03 class is imported for its seeding helper only; the alias keeps pytest
# from collecting its tests a second time under this module.
from tests.integration.test_alembic_migrations import (
    TestB03NeuralEdgesBackfillMigration as _B03,
)
from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

F31_REVISION = "f31_2724_event_dates"
PRIOR_HEAD = "f30_resource_management_grants"
INDEX = "idx_memories_event_start"
OWNER = "f31-migration-test"


def _insert_stmt(cast: str):
    return text(
        f"""
        INSERT INTO memories
          (id, user_id, workspace_id, summary, content, type, importance, confidence,
           scope, embedding_status, client, source, long_term,
           access_count, details)
        VALUES
          (:id, 'u1', gen_random_uuid(), 'event row', 'event row', 'event', 0.5, 1.0,
           'working', 'success', 'test', 'mcp_remember', false, 0,
           CAST(:details AS {cast}))
        """
    )


async def _insert(db_session, details: dict, *, cast: str = "JSON") -> uuid.UUID:
    mem_id = uuid.uuid4()
    await db_session.execute(_insert_stmt(cast), {"id": mem_id, "details": json.dumps(details)})
    return mem_id


async def _columns(db_session, mem_id: uuid.UUID) -> tuple[str | None, str | None]:
    row = (
        await db_session.execute(
            text("SELECT event_start, event_end FROM memories WHERE id = :id"), {"id": mem_id}
        )
    ).one()
    return row.event_start, row.event_end


@pytest.mark.asyncio
@pytest.mark.parametrize("cast", ["JSON", "JSONB"])
async def test_the_days_are_copied(db_session, cast):
    mem_id = await _insert(
        db_session,
        {"event_phrase": "last weekend", "event_start": "2024-03-09", "event_end": "2024-03-10"},
        cast=cast,
    )
    assert await _columns(db_session, mem_id) == ("2024-03-09", "2024-03-10")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "details",
    [
        {"other": 1},
        {"event_phrase": "a few days ago"},
        {"event_start": None, "event_end": None},
        {"event_start": "2024-3-9", "event_end": "2024/03/10"},
        {"event_start": "2024-03-09T00:00:00Z"},
        {"event_start": 20240309},
        {"event_start": {"nested": "2024-03-09"}},
    ],
)
async def test_anything_else_is_null_and_never_an_insert_error(db_session, details):
    mem_id = await _insert(db_session, details)
    assert await _columns(db_session, mem_id) == (None, None)


@pytest.mark.asyncio
async def test_a_start_alone_leaves_the_end_null_in_the_column(db_session):
    """The service always writes the pair; raw SQL with a start alone reads
    back as a start only — the readers treat that as a single day."""
    mem_id = await _insert(db_session, {"event_start": "2024-03-09"})
    assert await _columns(db_session, mem_id) == ("2024-03-09", None)


@pytest.mark.asyncio
async def test_the_columns_follow_a_details_update(db_session):
    mem_id = await _insert(db_session, {"event_start": "2024-03-09", "event_end": "2024-03-10"})
    await db_session.execute(
        text("UPDATE memories SET details = CAST(:d AS JSON) WHERE id = :id"),
        {"id": mem_id, "d": json.dumps({"event_start": "2025-01-02", "event_end": "2025-01-03"})},
    )
    assert await _columns(db_session, mem_id) == ("2025-01-02", "2025-01-03")
    await db_session.execute(
        text("UPDATE memories SET details = NULL WHERE id = :id"), {"id": mem_id}
    )
    assert await _columns(db_session, mem_id) == (None, None)


@pytest.mark.asyncio
async def test_lexical_order_is_chronological(db_session):
    ids = [
        await _insert(db_session, {"event_start": value})
        for value in ("2024-03-10", "2024-03-09", "2023-12-31")
    ]
    rows = (
        await db_session.execute(
            text("SELECT event_start FROM memories WHERE id = ANY(:ids) ORDER BY event_start"),
            {"ids": ids},
        )
    ).scalars()
    assert list(rows) == ["2023-12-31", "2024-03-09", "2024-03-10"]


# --------------------------------------------------------------- round trip


def _indexdef(conn: Any) -> str | None:
    return conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": INDEX}
    ).scalar_one_or_none()


def _index_valid(conn: Any) -> Any:
    return conn.execute(
        text(
            "SELECT i.indisvalid FROM pg_class c JOIN pg_index i "
            "ON c.oid = i.indexrelid WHERE c.relname = :n"
        ),
        {"n": INDEX},
    ).scalar()


def _column_names(conn: Any) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'memories' AND column_name IN ('event_start', 'event_end')"
        )
    ).scalars()
    return set(rows)


def _seed(conn: Any, ws_id: str, ctx_id: str, details: dict) -> str:
    mem_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO memories "
            "(id, user_id, workspace_id, context_id, summary, content, type, "
            " embedding_status, importance, confidence, scope, long_term, "
            " access_count, client, source, details) "
            "VALUES (:id, :u, :ws, :ctx, 's', 'c', 'event', 'success', "
            " 0.5, 1.0, 'working', false, 0, 'test', 'test', CAST(:details AS JSON))"
        ),
        {"id": mem_id, "u": OWNER, "ws": ws_id, "ctx": ctx_id, "details": json.dumps(details)},
    )
    return mem_id


def _leave_an_invalid_index(engine: Any) -> None:
    """A failed ``CREATE ... CONCURRENTLY`` leaves the index behind INVALID.

    A UNIQUE build over two same-day rows of one context fails on the
    duplicate the way an interrupted build does — no catalog write, no
    superuser needed.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY idx_memories_event_start "
                    "ON memories (context_id, event_start) "
                    "WHERE event_start IS NOT NULL AND deleted_at IS NULL"
                )
            )


def test_f31_round_trip_builds_the_columns_and_the_index() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            assert _column_names(conn) == set()
            assert _indexdef(conn) is None
            ws_id, ctx_id = _B03._seed_workspace_and_context(conn, OWNER)
            # Two events of the same day in one context, so the concurrent
            # build runs over data (and a UNIQUE one below fails on them).
            seeded = [
                _seed(conn, ws_id, ctx_id, {"event_start": "2024-03-09", "event_end": "2024-03-09"})
                for _ in range(2)
            ]
            untouched = _seed(conn, ws_id, ctx_id, {"observed_at": "2024-03-11"})

        _upgrade_to(F31_REVISION)
        with engine.connect() as conn:
            assert _column_names(conn) == {"event_start", "event_end"}
            indexdef = _indexdef(conn)
            assert indexdef is not None
            assert "(context_id, event_start)" in indexdef
            assert "event_start IS NOT NULL" in indexdef and "deleted_at IS NULL" in indexdef
            assert _index_valid(conn) is True
            for mem_id in seeded:
                row = conn.execute(
                    text("SELECT event_start, event_end FROM memories WHERE id = :id"),
                    {"id": mem_id},
                ).one()
                assert (row.event_start, row.event_end) == ("2024-03-09", "2024-03-09")
            row = conn.execute(
                text("SELECT event_start, event_end FROM memories WHERE id = :id"),
                {"id": untouched},
            ).one()
            assert (row.event_start, row.event_end) == (None, None)

        # Re-running the upgrade after an interrupted build: the INVALID
        # leftover (here a UNIQUE one) is dropped and the index rebuilt; the
        # ADD COLUMN IF NOT EXISTS steps are no-ops.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("DROP INDEX idx_memories_event_start"))
        _leave_an_invalid_index(engine)
        with engine.connect() as conn:
            assert _index_valid(conn) is False
        _downgrade_to(PRIOR_HEAD)  # alembic_version back; the INVALID index is dropped too
        with engine.connect() as conn:
            assert _column_names(conn) == set()
            assert _indexdef(conn) is None
        _upgrade_to(F31_REVISION)
        with engine.connect() as conn:
            assert _index_valid(conn) is True

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _column_names(conn) == set()
            assert _indexdef(conn) is None
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM memories WHERE user_id = :u"), {"u": OWNER})
            finally:
                engine.dispose()


def test_f31_rerun_over_an_invalid_leftover_rebuilds_the_index() -> None:
    """The upgrade's own rerun path: an INVALID index of the right name is
    dropped before the ``IF NOT EXISTS`` create, so the rebuilt one is VALID."""
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            ws_id, ctx_id = _B03._seed_workspace_and_context(conn, OWNER)
        _upgrade_to(F31_REVISION)
        with engine.begin() as conn:
            for _ in range(2):
                _seed(conn, ws_id, ctx_id, {"event_start": "2024-03-09", "event_end": "2024-03-09"})
        # Simulate the interrupted run: alembic back at f30 but the columns
        # kept (as a failed index build after the committed ALTERs leaves it).
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("DROP INDEX idx_memories_event_start"))
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": PRIOR_HEAD})
        _leave_an_invalid_index(engine)
        with engine.connect() as conn:
            assert _index_valid(conn) is False
        _upgrade_to(F31_REVISION)
        with engine.connect() as conn:
            assert _index_valid(conn) is True
            assert "UNIQUE" not in (_indexdef(conn) or "")
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM memories WHERE user_id = :u"), {"u": OWNER})
            finally:
                engine.dispose()
