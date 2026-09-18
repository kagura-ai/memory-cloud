"""DB contract for e81 (#1569): lane columns on ``memory_analyses``, nullable FK.

- upgrade lets a run row exist with ``model_id IS NULL`` and records
  ``llm_provider`` / ``llm_model``;
- downgrade refuses while such a row exists (NOT NULL could not be restored);
- once it is gone, downgrade drops the columns and restores NOT NULL.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PRE_E81_REV = "e80_1570_unseed_sh_zero_pricing"
E81_REV = "e81_1569_analysis_lane_cols"


def _seed_workspace_context(conn: Connection) -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"u-{suffix}"
    workspace_id = str(uuid.uuid4())
    context_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO users (email, user_id, role, timezone, locale, is_initial_admin)"
            " VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{user_id}@test.example", "uid": user_id},
    )
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": workspace_id, "name": f"ws-{suffix}", "owner": user_id},
    )
    conn.execute(
        text("INSERT INTO contexts (id, workspace_id, name) VALUES (:id, :ws, :name)"),
        {"id": context_id, "ws": workspace_id, "name": f"ctx-{suffix}"},
    )
    return user_id, workspace_id, context_id


def _column(conn: Connection, name: str) -> tuple[str, str] | None:
    row = conn.execute(
        text(
            "SELECT is_nullable, data_type FROM information_schema.columns"
            " WHERE table_name = 'memory_analyses' AND column_name = :name"
        ),
        {"name": name},
    ).one_or_none()
    return None if row is None else (row.is_nullable, row.data_type)


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE81AnalysisLaneColumns:
    def test_upgrade_allows_unpriced_managed_run_and_downgrade_guards_it(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E81_REV)
            engine = _sync_engine()

            with engine.begin() as conn:
                assert _column(conn, "model_id") == ("NO", "bigint")
                assert _column(conn, "llm_provider") is None
                assert _column(conn, "llm_model") is None

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E81_REV)

            with engine.begin() as conn:
                assert _column(conn, "model_id") == ("YES", "bigint")
                assert _column(conn, "llm_provider") == ("YES", "character varying")
                assert _column(conn, "llm_model") == ("YES", "character varying")
                user_id, workspace_id, context_id = _seed_workspace_context(conn)
                run_id = conn.execute(
                    text(
                        "INSERT INTO memory_analyses (workspace_id, context_id, triggered_by,"
                        " model_id, llm_provider, llm_model, model_snapshot, embedding_model,"
                        " params, input_count, paid_by)"
                        " VALUES (:ws, :ctx, :uid, NULL, 'self_hosted', 'qwen3:8b',"
                        " '{\"rates\": {}}'::jsonb, 'em', '{}'::jsonb, 0, 'platform')"
                        " RETURNING id"
                    ),
                    {"ws": workspace_id, "ctx": context_id, "uid": user_id},
                ).scalar_one()

            # Downgrade refuses while the unpriced row exists.
            with pytest.raises(RuntimeError, match="model_id IS NULL"):
                with _alembic_at_test_db():
                    command.downgrade(_get_alembic_config(), PRE_E81_REV)

            with engine.begin() as conn:
                conn.execute(text("DELETE FROM memory_analyses WHERE id = :id"), {"id": run_id})

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E81_REV)

            with engine.begin() as conn:
                assert _column(conn, "model_id") == ("NO", "bigint")
                assert _column(conn, "llm_provider") is None
                assert _column(conn, "llm_model") is None
        finally:
            _leave_db_at_head()
