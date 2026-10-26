"""Migration round-trip for f17_2298_audit_logs_user_id_idx (#2298).

1. upgrade builds ``ix_audit_logs_user_id`` — a plain btree on
   ``audit_logs (user_id)``, no WHERE — CONCURRENTLY, over existing audit
   rows, and leaves it VALID;
2. a second upgrade run over an INVALID leftover of the same name (what an
   interrupted concurrent build leaves behind) drops and rebuilds it;
3. downgrade drops it again.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

F17_REVISION = "f17_2298_audit_logs_user_id_idx"
PRIOR_HEAD = "f16_2330_observed_at"
INDEX = "ix_audit_logs_user_id"
EXPECTED_DEF = "CREATE INDEX ix_audit_logs_user_id ON public.audit_logs USING btree (user_id)"
ACTOR = "f17-migration-test"


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


def _seed(conn: Any) -> None:
    """Two audit rows by the same actor, in the f16 schema (explicit columns)."""
    conn.execute(
        text(
            "INSERT INTO audit_logs (user_email, user_id, action, resource) "
            "VALUES ('f17@example.com', :actor, 'f17_seed', 'f17'), "
            "('f17@example.com', :actor, 'f17_seed', 'f17')"
        ),
        {"actor": ACTOR},
    )


def _leave_an_invalid_index(engine: Any) -> None:
    """A failed ``CREATE ... CONCURRENTLY`` leaves the index behind INVALID.

    A UNIQUE build over the two same-actor rows fails on the duplicate the
    way an interrupted build does — no catalog write, no superuser needed.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY ix_audit_logs_user_id ON audit_logs (user_id)"
                )
            )


def test_f17_round_trip_builds_the_user_id_index() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            assert _indexdef(conn) is None
            # Seeded rows, so the concurrent build runs over data.
            _seed(conn)

        _upgrade_to(F17_REVISION)
        with engine.connect() as conn:
            assert _indexdef(conn) == EXPECTED_DEF
            assert _index_valid(conn) is True

        # Re-running the upgrade after an interrupted build: the INVALID
        # leftover (here a UNIQUE one) is dropped and the plain index rebuilt.
        _downgrade_to(PRIOR_HEAD)
        _leave_an_invalid_index(engine)
        with engine.connect() as conn:
            assert _index_valid(conn) is False
        _upgrade_to(F17_REVISION)
        with engine.connect() as conn:
            assert _indexdef(conn) == EXPECTED_DEF
            assert _index_valid(conn) is True

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _indexdef(conn) is None
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM audit_logs WHERE user_id = :actor"), {"actor": ACTOR}
                    )
            finally:
                engine.dispose()
