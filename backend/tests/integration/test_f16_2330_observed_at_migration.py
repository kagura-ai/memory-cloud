"""DB contract for f16_2330_observed_at (#2330).

The generated column: a date is padded to the start of its day, a UTC
datetime is copied, anything else is NULL and never an INSERT error (on both
the ``json`` and the ``jsonb`` insert path). The migration's legacy step
rewrites parseable non-canonical values and leaves the rest. The partial
index exists with its predicate, and downgrade drops both.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    TestB03NeuralEdgesBackfillMigration,
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_and_upgrade_to,
    _sync_engine,
)

PRE_F16_REV = "f15_2026_json_none_as_null"
F16_REV = "f16_2330_observed_at"


def _insert_stmt(cast: str):
    return text(
        f"""
        INSERT INTO memories
          (id, user_id, workspace_id, summary, content, type, importance, confidence,
           scope, embedding_status, client, source, long_term,
           access_count, details)
        VALUES
          (:id, 'u1', gen_random_uuid(), 'event row', 'event row', 'note', 0.5, 1.0,
           'working', 'success', 'test', 'mcp_remember', false, 0,
           CAST(:details AS {cast}))
        """
    )


async def _insert(db_session, details: dict | str, *, cast: str = "JSON") -> uuid.UUID:
    mem_id = uuid.uuid4()
    raw = details if isinstance(details, str) else json.dumps(details)
    await db_session.execute(_insert_stmt(cast), {"id": mem_id, "details": raw})
    return mem_id


async def _column(db_session, mem_id: uuid.UUID) -> str | None:
    return (
        await db_session.execute(
            text("SELECT observed_at FROM memories WHERE id = :id"), {"id": mem_id}
        )
    ).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("cast", ["JSON", "JSONB"])
async def test_date_is_padded_and_datetime_is_copied(db_session, cast):
    day = await _insert(db_session, {"observed_at": "2024-03-07"}, cast=cast)
    moment = await _insert(db_session, {"observed_at": "2024-03-07T10:00:00Z"}, cast=cast)
    assert await _column(db_session, day) == "2024-03-07T00:00:00Z"
    assert await _column(db_session, moment) == "2024-03-07T10:00:00Z"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "details",
    [
        {"other": 1},
        {"observed_at": None},
        {"observed_at": "2024/03/07"},
        {"observed_at": "March 7"},
        # The app would have normalised this to UTC; raw SQL gets NULL.
        {"observed_at": "2024-03-07T10:00:00+09:00"},
        {"observed_at": 20240307},
        {"observed_at": {"nested": "2024-03-07"}},
    ],
)
async def test_anything_else_is_null_and_never_an_insert_error(db_session, details):
    mem_id = await _insert(db_session, details)
    assert await _column(db_session, mem_id) is None


@pytest.mark.asyncio
async def test_the_column_follows_a_details_update(db_session):
    mem_id = await _insert(db_session, {"observed_at": "2024-03-07"})
    await db_session.execute(
        text("UPDATE memories SET details = CAST(:d AS JSON) WHERE id = :id"),
        {"id": mem_id, "d": json.dumps({"observed_at": "2025-01-02T03:04:05Z"})},
    )
    assert await _column(db_session, mem_id) == "2025-01-02T03:04:05Z"
    await db_session.execute(
        text("UPDATE memories SET details = NULL WHERE id = :id"), {"id": mem_id}
    )
    assert await _column(db_session, mem_id) is None


@pytest.mark.asyncio
async def test_lexical_order_is_chronological_across_both_forms(db_session):
    ids = [
        await _insert(db_session, {"observed_at": value})
        for value in ("2024-03-07T10:00:00Z", "2024-03-07", "2024-03-06T23:59:59Z")
    ]
    rows = (
        await db_session.execute(
            text("SELECT observed_at FROM memories WHERE id = ANY(:ids) ORDER BY observed_at"),
            {"ids": ids},
        )
    ).scalars()
    assert list(rows) == [
        "2024-03-06T23:59:59Z",
        "2024-03-07T00:00:00Z",
        "2024-03-07T10:00:00Z",
    ]


def _seed(conn: Connection, owner: str, ws_id: str, ctx_id: str, details: dict) -> str:
    mem_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO memories "
            "(id, user_id, workspace_id, context_id, summary, content, type, "
            " embedding_status, importance, confidence, scope, long_term, "
            " access_count, client, source, details) "
            "VALUES (:id, :u, :ws, :ctx, 's', 'c', 'note', 'success', "
            " 0.5, 1.0, 'working', false, 0, 'test', 'test', CAST(:details AS JSON))"
        ),
        {"id": mem_id, "u": owner, "ws": ws_id, "ctx": ctx_id, "details": json.dumps(details)},
    )
    return mem_id


def _load_migration():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "f16_2330_observed_at.py"
    spec = importlib.util.spec_from_file_location("f16_2330_observed_at", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


def _column_exists(conn: Connection) -> bool:
    return (
        conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'memories' AND column_name = 'observed_at'"
            )
        ).scalar_one()
        == 1
    )


def _index_def(conn: Connection) -> str | None:
    return conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_memories_observed_at'")
    ).scalar_one_or_none()


class TestF16ObservedAtMigration:
    def test_legacy_values_are_normalised_and_the_column_is_filled(self) -> None:
        try:
            _reset_and_upgrade_to(PRE_F16_REV)
            owner = "owner-2330-observed"
            cases = [
                # (details.observed_at before, after, column)
                ("2024-03-07", "2024-03-07", "2024-03-07T00:00:00Z"),
                ("2024-03-07T10:00:00Z", "2024-03-07T10:00:00Z", "2024-03-07T10:00:00Z"),
                ("2024-03-07T10:00:00+09:00", "2024-03-07T01:00:00Z", "2024-03-07T01:00:00Z"),
                ("2024-03-07T10:00:00.5", "2024-03-07T10:00:00Z", "2024-03-07T10:00:00Z"),
                ("2024-03-07\n", "2024-03-07", "2024-03-07T00:00:00Z"),
                # Not an accepted input form: left as it is, column NULL.
                ("2024-03-07 10:00:00.5", "2024-03-07 10:00:00.5", None),
                ("20240307", "20240307", None),
                ("March 7", "March 7", None),
            ]
            engine = _sync_engine()
            with engine.begin() as conn:
                ws_id, ctx_id = TestB03NeuralEdgesBackfillMigration._seed_workspace_and_context(
                    conn, owner
                )
                seeded = [
                    (
                        _seed(
                            conn,
                            owner,
                            ws_id,
                            ctx_id,
                            {"observed_at": before, "location": {"lat": 1.0, "lon": 5e-05}},
                        ),
                        after,
                        column,
                    )
                    for before, after, column in cases
                ]
                untouched = _seed(conn, owner, ws_id, ctx_id, {"other": 1})
            engine.dispose()

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), F16_REV)

            engine = _sync_engine()
            with engine.begin() as conn:
                for mem_id, after, column in seeded:
                    row = conn.execute(
                        text(
                            "SELECT details->>'observed_at' AS value, observed_at, "
                            "location_lon FROM memories WHERE id = :id"
                        ),
                        {"id": mem_id},
                    ).one()
                    assert row.value == after
                    assert row.observed_at == column
                    # The jsonb round trip of a rewritten row keeps the location.
                    assert row.location_lon == pytest.approx(5e-05)
                assert (
                    conn.execute(
                        text("SELECT observed_at FROM memories WHERE id = :id"),
                        {"id": untouched},
                    ).scalar_one()
                    is None
                )
                indexdef = _index_def(conn)
                assert indexdef is not None
                assert "(context_id, observed_at)" in indexdef
                assert "observed_at IS NOT NULL" in indexdef
                assert "deleted_at IS NULL" in indexdef
            engine.dispose()
        finally:
            _leave_db_at_head()

    def test_lock_timeout_does_not_outlive_the_migration(self) -> None:
        """The 5 s cap is reset at the end of upgrade / downgrade, so later
        migrations of the same run (one transaction) do not inherit it."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        try:
            _reset_and_upgrade_to(PRE_F16_REV)
            engine = _sync_engine()
            module = _load_migration()
            with engine.begin() as conn:
                default = conn.execute(text("SHOW lock_timeout")).scalar_one()
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    module.upgrade()
                    assert conn.execute(text("SHOW lock_timeout")).scalar_one() == default
                    module.downgrade()
                    assert conn.execute(text("SHOW lock_timeout")).scalar_one() == default
            engine.dispose()
        finally:
            _leave_db_at_head()

    def test_legacy_rewrite_runs_under_the_lock_timeout(self, monkeypatch) -> None:
        """The legacy row UPDATEs wait at most the cap too: a row held by a
        live writer during the deploy fails the migration instead of hanging."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        try:
            _reset_and_upgrade_to(PRE_F16_REV)
            engine = _sync_engine()
            module = _load_migration()
            seen: list[str] = []
            with engine.begin() as conn:
                monkeypatch.setattr(
                    module,
                    "_normalise_legacy_values",
                    lambda: seen.append(conn.execute(text("SHOW lock_timeout")).scalar_one()),
                )
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    module.upgrade()
                    module.downgrade()
            engine.dispose()
            assert seen == ["5s"]
        finally:
            _leave_db_at_head()

    def test_downgrade_drops_the_column_and_the_index(self) -> None:
        try:
            _reset_and_upgrade_to(F16_REV)
            engine = _sync_engine()
            with engine.begin() as conn:
                assert _column_exists(conn)
                assert _index_def(conn) is not None
            engine.dispose()

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_F16_REV)

            engine = _sync_engine()
            with engine.begin() as conn:
                assert not _column_exists(conn)
                assert _index_def(conn) is None
            engine.dispose()
        finally:
            _leave_db_at_head()
