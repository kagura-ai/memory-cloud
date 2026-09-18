"""DB contract for e80 (#1570): the seeded $0 ``self_hosted`` embedding rows go away.

- upgrade removes the five c03 seed rows (renamed by e53) …
- … except one still referenced by ``memory_analyses.model_id`` (RESTRICT FK);
- downgrade re-inserts the five rows at price 0 without duplicating the kept one.
"""

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PRE_E80_REV = "e79_1548_promax_plan_tier"
E80_REV = "e80_1570_unseed_sh_zero_pricing"

_SEED_EFFECTIVE_FROM = datetime(2026, 4, 28, 0, 0, 0)
_SEED_MODELS = {
    "nomic-embed-text",
    "mxbai-embed-large",
    "qwen3-embedding:0.6b",
    "qwen3-embedding:4b",
    "qwen3-embedding:8b",
}

_SEED_ROWS_SQL = text(
    "SELECT model, price_per_unit FROM llm_pricing"
    " WHERE provider = 'self_hosted' AND unit_type = 'embedding_tokens'"
    " AND effective_from = :effective_from ORDER BY model"
)


def _seed_rows(conn: Connection) -> dict[str, float]:
    rows = conn.execute(_SEED_ROWS_SQL, {"effective_from": _SEED_EFFECTIVE_FROM}).all()
    return {row.model: float(row.price_per_unit) for row in rows}


def _seed_analysis_referencing(conn: Connection, model: str) -> int:
    """Create a minimal memory_analyses row whose model_id points at ``model``'s seed row."""
    pricing_id = conn.execute(
        text(
            "SELECT id FROM llm_pricing WHERE provider = 'self_hosted' AND model = :model"
            " AND unit_type = 'embedding_tokens' AND effective_from = :effective_from"
        ),
        {"model": model, "effective_from": _SEED_EFFECTIVE_FROM},
    ).scalar_one()
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
    conn.execute(
        text(
            "INSERT INTO memory_analyses (workspace_id, context_id, triggered_by, model_id,"
            " model_snapshot, embedding_model, params, input_count)"
            " VALUES (:ws, :ctx, :uid, :model_id, '{}'::jsonb, :embedding_model, '{}'::jsonb, 0)"
        ),
        {
            "ws": workspace_id,
            "ctx": context_id,
            "uid": user_id,
            "model_id": pricing_id,
            "embedding_model": model,
        },
    )
    return pricing_id


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE80UnseedSelfHostedZeroPricing:
    def test_upgrade_removes_unreferenced_rows_and_downgrade_restores_them(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E80_REV)
            engine = _sync_engine()

            with engine.begin() as conn:
                before = _seed_rows(conn)
                assert set(before) == _SEED_MODELS, before
                assert set(before.values()) == {0.0}
                kept_id = _seed_analysis_referencing(conn, "qwen3-embedding:4b")

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E80_REV)

            with engine.begin() as conn:
                after = _seed_rows(conn)
                # The referenced row survives (RESTRICT FK); the other four are gone.
                assert after == {"qwen3-embedding:4b": 0.0}, after
                still_there = conn.execute(
                    text("SELECT id FROM llm_pricing WHERE id = :id"), {"id": kept_id}
                ).scalar_one_or_none()
                assert still_there == kept_id

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E80_REV)

            with engine.begin() as conn:
                restored = _seed_rows(conn)
                assert set(restored) == _SEED_MODELS, restored
                assert set(restored.values()) == {0.0}
                # ON CONFLICT DO NOTHING — the kept row was not duplicated.
                count = conn.execute(
                    text(
                        "SELECT count(*) FROM llm_pricing WHERE provider = 'self_hosted'"
                        " AND model = 'qwen3-embedding:4b' AND unit_type = 'embedding_tokens'"
                    )
                ).scalar_one()
                assert count == 1
        finally:
            _leave_db_at_head()
