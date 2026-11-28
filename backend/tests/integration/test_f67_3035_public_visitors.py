"""#3035 (f67): route and caller on usage events, the daily visitor table, a counts index.

1. The upgrade adds ``caller`` VARCHAR(16) and ``route`` VARCHAR(8), both
   nullable with no default, with their CHECKs validated; creates
   ``public_mcp_daily_visitors`` (primary key ``endpoint_id, day,
   visitor_key``, the key-shape CHECK, ``ix_pmdv_day``); and builds
   ``ix_pmue_endpoint_counts`` valid, with its key and INCLUDE columns and
   its predicate. It takes no lock on another table: it runs while
   ``users``, ``workspaces`` and ``contexts`` are held in ``ACCESS
   EXCLUSIVE`` by a transaction that ran only ``LOCK TABLE``.
2. An INSERT that names neither column (the previous release) stores NULL;
   an unknown caller or route and a visitor key that is not 32 hex are
   refused.
3. A rerun after an interrupted upgrade (the index left INVALID) rebuilds
   the index; downgrade deletes the REST rows over more than one batch,
   keeps the others, and drops the rest; the round trip is repeatable.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _downgrade_to,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f67_3035_public_visitors"
BEFORE = "f66_3019_sleep_coverage"
INDEX = "ix_pmue_endpoint_counts"
BATCH = 5_000

_TOOL_CALL = (
    "INSERT INTO public_mcp_usage_events (occurred_at, kind, endpoint_id, workspace_id, "
    "context_id, resource_pk, resource_id, tool, outcome, era, duration_ms) "
    "VALUES (now(), 'tool_call', :e, :w, :c, :r, 'catalog', 'recall', 'success', "
    "'stateless', 3) RETURNING id"
)


def _sqlstate(exc: IntegrityError) -> str | None:
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


def _columns(conn: Connection) -> dict[str, tuple[str, str, int | None, str | None]]:
    rows = conn.execute(
        text(
            "SELECT column_name, is_nullable, data_type, character_maximum_length, "
            "column_default FROM information_schema.columns "
            "WHERE table_name = 'public_mcp_usage_events' "
            "AND column_name IN ('caller', 'route')"
        )
    ).all()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def _checks(conn: Connection) -> dict[str, tuple[str, bool]]:
    rows = conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint "
            "WHERE conname IN ('valid_pmue_caller', 'valid_pmue_route', "
            "'pmdv_visitor_key_shape')"
        )
    ).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _index(conn: Connection) -> tuple[bool, list[str], int, str] | None:
    row = conn.execute(
        text(
            "SELECT ix.indisvalid, ARRAY(SELECT pg_get_indexdef(ix.indexrelid, k.ord::int, true) "
            "FROM generate_series(1, ix.indnatts) WITH ORDINALITY AS k(_, ord) ORDER BY k.ord), "
            "ix.indnkeyatts, pg_get_expr(ix.indpred, ix.indrelid) "
            "FROM pg_index ix JOIN pg_class i ON i.oid = ix.indexrelid WHERE i.relname = :n"
        ),
        {"n": INDEX},
    ).first()
    return None if row is None else (row[0], list(row[1]), row[2], row[3])


def _visitor_table(conn: Connection) -> tuple[list[str], list[str]] | None:
    if conn.scalar(text("SELECT to_regclass('public.public_mcp_daily_visitors')")) is None:
        return None
    pk = conn.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'public_mcp_daily_visitors'::regclass AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)"
        )
    ).scalars()
    indexes = conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = 'public_mcp_daily_visitors'")
    ).scalars()
    return list(pk), sorted(indexes)


def _insert(conn: Connection, ids: dict[str, uuid.UUID]) -> int:
    return int(conn.execute(text(_TOOL_CALL), ids).scalar_one())


def test_f67_round_trip_without_locking_other_tables() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    ids = {"e": uuid.uuid4(), "w": uuid.uuid4(), "c": uuid.uuid4(), "r": uuid.uuid4()}
    try:
        _upgrade_to(BEFORE)
        with engine.begin() as conn:
            assert _columns(conn) == {}
            assert _visitor_table(conn) is None
            existing = _insert(conn, ids)

        with holder.connect() as lock_conn:
            lock_tx = lock_conn.begin()
            lock_conn.execute(
                text("LOCK TABLE users, workspaces, contexts IN ACCESS EXCLUSIVE MODE")
            )
            try:
                _upgrade_to(REVISION)
            finally:
                lock_tx.rollback()

        with engine.begin() as conn:
            assert _columns(conn) == {
                "caller": ("YES", "character varying", 16, None),
                "route": ("YES", "character varying", 8, None),
            }
            checks = _checks(conn)
            assert set(checks) == {
                "valid_pmue_caller",
                "valid_pmue_route",
                "pmdv_visitor_key_shape",
            }
            assert all(validated for _, validated in checks.values())
            assert all("NOT VALID" not in definition for definition, _ in checks.values())
            for value in ("'anonymous'", "'bound_key'"):
                assert value in checks["valid_pmue_caller"][0]
            for value in ("'mcp'", "'rest'"):
                assert value in checks["valid_pmue_route"][0]
            assert "'^[0-9a-f]{32}$'" in checks["pmdv_visitor_key_shape"][0]
            assert _visitor_table(conn) == (
                ["endpoint_id", "day", "visitor_key"],
                ["ix_pmdv_day", "public_mcp_daily_visitors_pkey"],
            )
            assert _index(conn) == (
                True,
                [
                    "endpoint_id",
                    "occurred_at",
                    "workspace_id",
                    "kind",
                    "tool",
                    "outcome",
                    "route",
                    "caller",
                ],
                2,
                "(endpoint_id IS NOT NULL)",
            )
            row = conn.execute(
                text("SELECT caller, route FROM public_mcp_usage_events WHERE id = :id"),
                {"id": existing},
            ).one()
            assert tuple(row) == (None, None), "an existing row reads NULL, no backfill"
            # The previous release's INSERT names neither column.
            later = _insert(conn, ids)
            row = conn.execute(
                text("SELECT caller, route FROM public_mcp_usage_events WHERE id = :id"),
                {"id": later},
            ).one()
            assert tuple(row) == (None, None)

        for statement in (
            "UPDATE public_mcp_usage_events SET caller = 'x' WHERE id = :id",
            "UPDATE public_mcp_usage_events SET route = 'x' WHERE id = :id",
        ):
            with pytest.raises(IntegrityError) as refused, engine.begin() as conn:
                conn.execute(text(statement), {"id": existing})
            assert _sqlstate(refused.value) == "23514"
        with pytest.raises(IntegrityError) as refused, engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO public_mcp_daily_visitors (endpoint_id, day, visitor_key, "
                    "workspace_id) VALUES (:e, current_date, :k, :w)"
                ),
                {"e": ids["e"], "w": ids["w"], "k": "vis-value-that-is-not-a-key-0123"},
            )
        assert _sqlstate(refused.value) == "23514"

        # An interrupted upgrade: the index left INVALID, the version back
        # at f66. Running it again rebuilds the index.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE pg_index SET indisvalid = false "
                    "WHERE indexrelid = 'ix_pmue_endpoint_counts'::regclass"
                )
            )
        with _alembic_at_test_db():
            command.stamp(_get_alembic_config(), BEFORE)
        _upgrade_to(REVISION)
        with engine.begin() as conn:
            index = _index(conn)
            assert index is not None and index[0] is True

            # More REST rows than one downgrade batch, and rows to keep.
            conn.execute(
                text(
                    "INSERT INTO public_mcp_usage_events (occurred_at, kind, endpoint_id, "
                    "workspace_id, context_id, resource_pk, resource_id, tool, outcome, era, "
                    "duration_ms, route, caller) "
                    "SELECT now(), 'tool_call', :e, :w, :c, :r, 'catalog', 'recall', "
                    "'success', 'stateless', 1, 'rest', 'bound_key' "
                    "FROM generate_series(1, :n)"
                ),
                {**ids, "n": BATCH + 1},
            )
            mcp = int(
                conn.execute(
                    text(
                        "INSERT INTO public_mcp_usage_events (occurred_at, kind, endpoint_id, "
                        "workspace_id, context_id, resource_pk, resource_id, tool, outcome, "
                        "era, duration_ms, route, caller) VALUES (now(), 'tool_call', :e, :w, "
                        ":c, :r, 'catalog', 'recall', 'success', 'session', 1, 'mcp', "
                        "'anonymous') RETURNING id"
                    ),
                    ids,
                ).scalar_one()
            )

        _downgrade_to(BEFORE)
        with engine.begin() as conn:
            assert _columns(conn) == {}
            assert _checks(conn) == {}
            assert _visitor_table(conn) is None
            assert _index(conn) is None
            kept = set(
                conn.execute(
                    text("SELECT id FROM public_mcp_usage_events WHERE endpoint_id = :e"),
                    {"e": ids["e"]},
                ).scalars()
            )
            assert kept == {existing, later, mcp}

        _upgrade_to(REVISION)
        with engine.begin() as conn:
            assert set(_columns(conn)) == {"caller", "route"}
            assert _index(conn) is not None
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM public_mcp_usage_events WHERE endpoint_id = :e"),
                    {"e": ids["e"]},
                )
        finally:
            engine.dispose()
            holder.dispose()
