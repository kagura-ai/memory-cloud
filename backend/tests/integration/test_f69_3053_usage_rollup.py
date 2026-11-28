"""#3053 (f69): the per-day counts of public usage events and the closed days.

1. The upgrade creates ``public_mcp_usage_day_counts`` (primary key
   ``endpoint_id, day, workspace_id, tool, outcome, route, caller``, every
   column NOT NULL, ``calls`` a positive bigint, the purge index
   ``ix_pmudc_day``) and ``public_mcp_usage_closed_days`` (primary key
   ``day``, ``closed_at``). It counts nothing (closing days is the hourly
   job's) and takes no lock on another table: it runs while ``users``,
   ``workspaces``, ``contexts`` and ``public_mcp_usage_events`` are held in
   ``ACCESS EXCLUSIVE`` by a transaction that ran only ``LOCK TABLE``.
2. A count that is not positive is refused.
3. A rerun with the version stamped back at f68 succeeds and leaves the same
   schema; downgrade drops both tables; the round trip is repeatable.

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

REVISION = "f69_3053_usage_rollup"
BEFORE = "f68_3051_carried_text_edit"
COUNTS = "public_mcp_usage_day_counts"
CLOSED = "public_mcp_usage_closed_days"


def _sqlstate(exc: IntegrityError) -> str | None:
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


def _exists(conn: Connection, table: str) -> bool:
    return conn.scalar(text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}) is not None


def _columns(conn: Connection, table: str) -> dict[str, tuple[str, str]]:
    rows = conn.execute(
        text(
            "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name = :t"
        ),
        {"t": table},
    ).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _primary_key(conn: Connection, table: str) -> list[str]:
    return list(
        conn.execute(
            text(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary "
                "ORDER BY array_position(i.indkey, a.attnum)"
            ),
            {"t": table},
        ).scalars()
    )


def _indexes(conn: Connection, table: str) -> list[str]:
    return sorted(
        conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table}
        ).scalars()
    )


def _schema(conn: Connection) -> tuple:
    return (
        _columns(conn, COUNTS),
        _primary_key(conn, COUNTS),
        _indexes(conn, COUNTS),
        _columns(conn, CLOSED),
        _primary_key(conn, CLOSED),
        _indexes(conn, CLOSED),
    )


EXPECTED = (
    {
        "endpoint_id": ("NO", "uuid"),
        "day": ("NO", "date"),
        "workspace_id": ("NO", "uuid"),
        "tool": ("NO", "character varying"),
        "outcome": ("NO", "character varying"),
        "route": ("NO", "character varying"),
        "caller": ("NO", "character varying"),
        "calls": ("NO", "bigint"),
    },
    ["endpoint_id", "day", "workspace_id", "tool", "outcome", "route", "caller"],
    ["ix_pmudc_day", "public_mcp_usage_day_counts_pkey"],
    {"day": ("NO", "date"), "closed_at": ("NO", "timestamp without time zone")},
    ["day"],
    ["public_mcp_usage_closed_days_pkey"],
)


def test_f69_round_trip_without_locking_other_tables() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    endpoint = uuid.uuid4()
    try:
        _upgrade_to(BEFORE)
        with engine.begin() as conn:
            assert not _exists(conn, COUNTS)
            assert not _exists(conn, CLOSED)

        with holder.connect() as lock_conn:
            lock_tx = lock_conn.begin()
            lock_conn.execute(
                text(
                    "LOCK TABLE users, workspaces, contexts, public_mcp_usage_events "
                    "IN ACCESS EXCLUSIVE MODE"
                )
            )
            try:
                _upgrade_to(REVISION)
            finally:
                lock_tx.rollback()

        with engine.begin() as conn:
            assert _schema(conn) == EXPECTED
            assert conn.scalar(text("SELECT count(*) FROM public_mcp_usage_day_counts")) == 0
            assert conn.scalar(text("SELECT count(*) FROM public_mcp_usage_closed_days")) == 0

        for calls in (0, -1):
            with pytest.raises(IntegrityError) as refused, engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO public_mcp_usage_day_counts (endpoint_id, day, "
                        "workspace_id, tool, outcome, route, caller, calls) VALUES (:e, current_date, :e, 'recall', "
                        "'success', 'mcp', 'anonymous', :n)"
                    ),
                    {"e": endpoint, "n": calls},
                )
            assert _sqlstate(refused.value) == "23514"

        # An interrupted upgrade: the version back at f68 with the schema in
        # place. Running it again succeeds and leaves the same schema.
        with _alembic_at_test_db():
            command.stamp(_get_alembic_config(), BEFORE)
        _upgrade_to(REVISION)
        with engine.begin() as conn:
            assert _schema(conn) == EXPECTED

        _downgrade_to(BEFORE)
        with engine.begin() as conn:
            assert not _exists(conn, COUNTS)
            assert not _exists(conn, CLOSED)

        _upgrade_to(REVISION)
        with engine.begin() as conn:
            assert _schema(conn) == EXPECTED
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
            holder.dispose()
