"""#2984 (f61): the keyword field lengths on ``memories``, five nullable columns.

1. The upgrade adds the five INTEGER NULL columns without a default and takes
   no lock on another table: it runs while ``contexts``, ``workspaces`` and
   ``users`` are held in ``ACCESS EXCLUSIVE`` by another transaction.
2. A rerun after an interrupted upgrade is a no-op; downgrade drops the five
   columns.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

from sqlalchemy import text

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f61_2984_keyword_lengths"
BEFORE = "f60_2979_domain_proof_via"
COLUMNS = (
    "keyword_summary_len",
    "keyword_context_summary_len",
    "keyword_content_len",
    "keyword_reading_len",
    "keyword_fulltext_len",
)


def _columns(conn) -> dict[str, tuple[str, str, str | None]]:
    rows = conn.execute(
        text(
            "SELECT column_name, is_nullable, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'memories' AND column_name LIKE 'keyword\\_%'"
        )
    ).all()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def test_f61_round_trip_without_locking_other_tables() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    try:
        _upgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _columns(conn) == {}

        with holder.connect() as lock_conn:
            lock_tx = lock_conn.begin()
            lock_conn.execute(
                text("LOCK TABLE contexts, workspaces, users IN ACCESS EXCLUSIVE MODE")
            )
            try:
                _upgrade_to(REVISION)
            finally:
                lock_tx.rollback()

        with engine.connect() as conn:
            assert _columns(conn) == dict.fromkeys(COLUMNS, ("YES", "integer", None))

        # A rerun after an interrupted upgrade is a no-op.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert set(_columns(conn)) == set(COLUMNS)

        _downgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _columns(conn) == {}
        _upgrade_to(REVISION)
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
            holder.dispose()
