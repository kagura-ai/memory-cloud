"""Integration tests for migration ``e16_709_embedding_spend_cap`` (#709).

Covers the data-shape behaviour that ``TestAlembicMigrations`` does not —
specifically:

1. Both ``workspaces.embedding_daily_cap_usd`` and
   ``workspaces.embedding_monthly_cap_usd`` columns are added; existing
   rows default to ``NULL`` (inherit-tier-default sentinel).
2. The two ``CHECK (... IS NULL OR ... >= 0)`` constraints accept:
   - ``NULL`` (no override),
   - ``0`` (admin explicitly disables embedding for the workspace),
   - positive values up to the column's ``NUMERIC(10, 6)`` precision.
3. The CHECK constraints REJECT negative values.
4. ``downgrade()`` drops both columns AND both constraints.

Pre-revision is ``e15_675_workspace_slot_bonus`` — the migration head
just before ``e16_709``.

Mirrors the structure of ``test_e15_675_workspace_slot_bonus_migration.py``.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

# Pre-e16 revision: state where the cap columns do NOT yet exist. Pinned
# so the test stays correct as more migrations land on top of e16.
PRE_E16_REV = "e15_675_workspace_slot_bonus"


def _seed_user(conn, user_id: str | None = None) -> str:
    """Insert a minimal user row; return the user_id (OAuth sub)."""
    uid = user_id or f"u-{uuid.uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO users "
            "(email, user_id, role, timezone, locale, is_initial_admin) "
            "VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{uid}@test.example", "uid": uid},
    )
    return uid


def _seed_workspace(conn, owner_user_id: str) -> str:
    """Insert a workspace owned by ``owner_user_id``."""
    ws_id = str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": ws_id, "name": f"ws-{ws_id[:8]}", "owner": owner_user_id},
    )
    return ws_id


def _get_caps(conn, ws_id: str) -> tuple[Decimal | None, Decimal | None]:
    row = conn.execute(
        text(
            "SELECT embedding_daily_cap_usd, embedding_monthly_cap_usd "
            "FROM workspaces WHERE id = :id"
        ),
        {"id": ws_id},
    ).one()
    return row[0], row[1]


def _leave_db_at_head() -> None:
    """Convention: integration suite expects the test DB at head after each test."""
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE16EmbeddingSpendCapMigration:
    """Data-shape and CHECK-constraint behaviour for e16_709."""

    def test_upgrade_adds_both_cap_columns_with_null_default(self):
        """Both cap columns exist after upgrade; existing workspaces default to NULL."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E16_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn)
                ws_id = _seed_workspace(conn, uid)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            inspector = inspect(engine)
            cols = {c["name"] for c in inspector.get_columns("workspaces")}
            assert "embedding_daily_cap_usd" in cols
            assert "embedding_monthly_cap_usd" in cols

            with engine.begin() as conn:
                daily, monthly = _get_caps(conn, ws_id)
                # NULL = "inherit tier default" — sentinel for the
                # ``Workspace.effective_*_cap_usd`` resolution order.
                assert daily is None
                assert monthly is None
        finally:
            engine.dispose()

    def test_check_constraint_accepts_null_zero_and_positive(self):
        """CHECK accepts NULL (no override), 0 (cap=disable), and positive USD values."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn)
                ws_id = _seed_workspace(conn, uid)

            with engine.begin() as conn:
                # NULL stays NULL on update — explicit no-op write
                conn.execute(
                    text(
                        "UPDATE workspaces SET embedding_daily_cap_usd = NULL, "
                        "embedding_monthly_cap_usd = NULL WHERE id = :id"
                    ),
                    {"id": ws_id},
                )
                assert _get_caps(conn, ws_id) == (None, None)

                # Zero is allowed: an admin explicitly disabling embedding
                # for one workspace WITHOUT changing the tier default.
                conn.execute(
                    text(
                        "UPDATE workspaces SET embedding_daily_cap_usd = 0, "
                        "embedding_monthly_cap_usd = 0 WHERE id = :id"
                    ),
                    {"id": ws_id},
                )
                daily, monthly = _get_caps(conn, ws_id)
                assert daily == Decimal("0")
                assert monthly == Decimal("0")

                # Typical positive override (Pro-tier-shaped: $5/day, $150/mo).
                conn.execute(
                    text(
                        "UPDATE workspaces SET embedding_daily_cap_usd = 5.25, "
                        "embedding_monthly_cap_usd = 150.75 WHERE id = :id"
                    ),
                    {"id": ws_id},
                )
                daily, monthly = _get_caps(conn, ws_id)
                assert daily == Decimal("5.25")
                assert monthly == Decimal("150.75")
        finally:
            engine.dispose()

    def test_check_constraint_rejects_negative_daily(self):
        """CHECK rejects a negative daily cap — defense-in-depth backstop for routes."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn)
                ws_id = _seed_workspace(conn, uid)

            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "UPDATE workspaces SET embedding_daily_cap_usd = -0.01 WHERE id = :id"
                        ),
                        {"id": ws_id},
                    )
        finally:
            engine.dispose()

    def test_check_constraint_rejects_negative_monthly(self):
        """CHECK rejects a negative monthly cap (separate constraint from daily)."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn)
                ws_id = _seed_workspace(conn, uid)

            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(
                        text("UPDATE workspaces SET embedding_monthly_cap_usd = -1 WHERE id = :id"),
                        {"id": ws_id},
                    )
        finally:
            engine.dispose()

    def test_downgrade_drops_columns_and_constraints(self):
        """Downgrade removes both columns AND both CHECK constraints."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
            command.downgrade(_get_alembic_config(), PRE_E16_REV)

        engine = _sync_engine()
        try:
            inspector = inspect(engine)
            cols = {c["name"] for c in inspector.get_columns("workspaces")}
            assert "embedding_daily_cap_usd" not in cols
            assert "embedding_monthly_cap_usd" not in cols

            # CHECK constraints disappear with the columns — verify the
            # constraints by name so a future migration that re-introduces
            # them under a different name doesn't false-pass here.
            check_constraints = {c["name"] for c in inspector.get_check_constraints("workspaces")}
            assert "embedding_daily_cap_usd_nonneg" not in check_constraints
            assert "embedding_monthly_cap_usd_nonneg" not in check_constraints
        finally:
            engine.dispose()
            _leave_db_at_head()
