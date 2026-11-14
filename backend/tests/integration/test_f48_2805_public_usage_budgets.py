"""Real Alembic upgrade/downgrade for the public usage budget table (#2805)."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f48_2805_public_usage_budgets"


def _prior() -> str:
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "f48_2805_public_usage_budgets.py"
    )
    spec = importlib.util.spec_from_file_location("f48_2805", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == REVISION
    return module.down_revision


def _seed_workspace(conn):
    workspace_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, created_at) "
            "VALUES (:id, 'ws', 'owner-1', now())"
        ),
        {"id": workspace_id},
    )
    return workspace_id


def _insert(conn, workspace_id, *, used: int = 1, seq: int | None = None) -> None:
    if seq is None:
        conn.execute(
            text(
                "INSERT INTO public_usage_budgets "
                "(workspace_id, period_started_at, calls_used, updated_at) "
                "VALUES (:ws, now(), :used, now())"
            ),
            {"ws": workspace_id, "used": used},
        )
    else:
        conn.execute(
            text(
                "INSERT INTO public_usage_budgets "
                "(workspace_id, period_started_at, calls_used, period_seq, updated_at) "
                "VALUES (:ws, now(), :used, :seq, now())"
            ),
            {"ws": workspace_id, "used": used, "seq": seq},
        )


def test_f48_creates_public_usage_budgets_and_downgrades_cleanly():
    prior = _prior()
    assert prior == "f47_2819_merge_sync_credentials"
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(prior)
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            columns = {
                row.column_name: (row.data_type, row.is_nullable)
                for row in conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_name='public_usage_budgets'"
                    )
                ).all()
            }
            assert columns == {
                "workspace_id": ("uuid", "NO"),
                "period_started_at": ("timestamp without time zone", "NO"),
                "calls_used": ("integer", "NO"),
                "period_seq": ("bigint", "NO"),
                "updated_at": ("timestamp without time zone", "NO"),
            }
            fks = {
                (row.ref_table, row.delete_rule)
                for row in conn.execute(
                    text(
                        "SELECT ccu.table_name AS ref_table, rc.delete_rule "
                        "FROM information_schema.referential_constraints rc "
                        "JOIN information_schema.table_constraints tc "
                        "  ON tc.constraint_name = rc.constraint_name "
                        "JOIN information_schema.constraint_column_usage ccu "
                        "  ON ccu.constraint_name = rc.unique_constraint_name "
                        "WHERE tc.table_name = 'public_usage_budgets'"
                    )
                ).all()
            }
            assert fks == {("workspaces", "CASCADE")}
            pk = conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = 'public_usage_budgets'::regclass AND i.indisprimary"
                )
            ).all()
            assert [row.attname for row in pk] == ["workspace_id"]
            indexes = conn.execute(
                text("SELECT count(*) FROM pg_indexes WHERE tablename = 'public_usage_budgets'")
            ).scalar_one()
            assert indexes == 1  # the primary key only
            checks = {
                row.conname
                for row in conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'public_usage_budgets'::regclass AND contype = 'c'"
                    )
                ).all()
            }
            assert checks == {
                "ck_public_usage_budgets_calls_used",
                "ck_public_usage_budgets_period_seq",
            }

        with engine.begin() as conn:
            workspace_id = _seed_workspace(conn)
            _insert(conn, workspace_id)
            seq = conn.execute(
                text("SELECT period_seq FROM public_usage_budgets WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            ).scalar_one()
            assert seq == 1  # server default

        for used, seq in ((-1, 1), (0, 0)):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                _insert(conn, _seed_workspace(conn), used=used, seq=seq)

        # The workspace's hard delete removes the row.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id})
            assert (
                conn.execute(
                    text("SELECT count(*) FROM public_usage_budgets WHERE workspace_id = :ws"),
                    {"ws": workspace_id},
                ).scalar_one()
                == 0
            )
            _insert(conn, _seed_workspace(conn))

        _downgrade_to(prior)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('public.public_usage_budgets')")).scalar()
                is None
            )

        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM public_usage_budgets")).scalar_one() == 0
    finally:
        engine.dispose()
        _reset_alembic_state()
        _upgrade_to("head")
