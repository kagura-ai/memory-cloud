"""#2980 (f59): ``context_search_configs.fusion``, a column with a constant default.

1. The upgrade adds ``fusion`` VARCHAR(20) NOT NULL DEFAULT 'auto' with the
   CHECK ``fusion_check`` (three values) and takes no lock on another table:
   it runs while ``contexts``, ``workspaces`` and ``users`` are held in
   ``ACCESS EXCLUSIVE`` by another transaction.
2. A row that existed reads ``auto``; an INSERT that does not name the column
   (the previous release's code) gets ``auto``; a value outside the three is
   refused by the CHECK.
3. A rerun after an interrupted upgrade is a no-op; downgrade drops the column
   and the CHECK, and the upgrade runs again.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f59_2980_context_fusion"
BEFORE = "f58_2941_domain_tls_reports"

_INSERT_CONFIG = (
    "INSERT INTO context_search_configs "
    "(context_id, semantic_weight, bm25_weight, fetch_factor, use_rerank, "
    "embedding_model, embedding_dimensions) "
    "VALUES (:id, 0.6, 0.4, 3, false, 'text-embedding-3-small', 512)"
)


def _column(conn) -> tuple[str, str, int | None, str | None] | None:
    row = conn.execute(
        text(
            "SELECT is_nullable, data_type, character_maximum_length, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'context_search_configs' AND column_name = 'fusion'"
        )
    ).first()
    return tuple(row) if row is not None else None


def _check(conn) -> str | None:
    return conn.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'fusion_check' AND conrelid = 'context_search_configs'::regclass"
        )
    )


def _context(conn, workspace_id: uuid.UUID) -> uuid.UUID:
    ctx_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) "
            "VALUES (:id, :ws, :name, 'f59-migration-test')"
        ),
        {"id": ctx_id, "ws": workspace_id, "name": f"f59-{ctx_id.hex[:8]}"},
    )
    return ctx_id


def _sqlstate(exc: IntegrityError) -> str | None:
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


def test_f59_round_trip_defaults_auto_without_locking_other_tables() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    workspace_id = uuid.uuid4()
    try:
        _upgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _column(conn) is None
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, 'f59-migration-test', 'free')"
                ),
                {"id": workspace_id, "name": f"f59-{workspace_id.hex[:8]}"},
            )
            existing = _context(conn, workspace_id)
            conn.execute(text(_INSERT_CONFIG), {"id": existing})
            fresh = _context(conn, workspace_id)
            refused = _context(conn, workspace_id)
            conn.commit()

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
            nullable, data_type, length, default = _column(conn)
            assert (nullable, data_type, length) == ("NO", "character varying", 20)
            assert default is not None and "'auto'" in default
            check = _check(conn)
            assert check is not None and "NOT VALID" not in check
            for value in ("auto", "rrf", "weighted"):
                assert f"'{value}'" in check
            assert (
                conn.scalar(
                    text("SELECT fusion FROM context_search_configs WHERE context_id = :id"),
                    {"id": existing},
                )
                == "auto"
            ), "an existing row reads auto, no backfill"
            # The previous release's INSERT does not name the column.
            conn.execute(text(_INSERT_CONFIG), {"id": fresh})
            assert (
                conn.scalar(
                    text("SELECT fusion FROM context_search_configs WHERE context_id = :id"),
                    {"id": fresh},
                )
                == "auto"
            )
            conn.execute(
                text("UPDATE context_search_configs SET fusion = 'rrf' WHERE context_id = :id"),
                {"id": fresh},
            )
            conn.commit()

        with pytest.raises(IntegrityError) as rejected, engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO context_search_configs "
                    "(context_id, semantic_weight, bm25_weight, fetch_factor, use_rerank, "
                    "embedding_model, embedding_dimensions, fusion) "
                    "VALUES (:id, 0.6, 0.4, 3, false, 'text-embedding-3-small', 512, "
                    "'keyword_floor')"
                ),
                {"id": refused},
            )
        assert _sqlstate(rejected.value) == "23514"

        # A rerun after an interrupted upgrade: the column exists, the revision
        # is not recorded. The upgrade is a no-op and keeps the stored values.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert (
                conn.scalar(
                    text("SELECT fusion FROM context_search_configs WHERE context_id = :id"),
                    {"id": fresh},
                )
                == "rrf"
            )

        _downgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _column(conn) is None
            assert _check(conn) is None
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _column(conn) is not None
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "DELETE FROM context_search_configs WHERE context_id IN "
                        "(SELECT id FROM contexts WHERE workspace_id = :ws)"
                    ),
                    {"ws": workspace_id},
                )
                conn.execute(
                    text("DELETE FROM contexts WHERE workspace_id = :ws"), {"ws": workspace_id}
                )
                conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
                conn.commit()
        finally:
            engine.dispose()
            holder.dispose()
