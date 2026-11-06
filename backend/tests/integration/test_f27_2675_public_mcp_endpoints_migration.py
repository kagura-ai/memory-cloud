"""Real Alembic upgrade/downgrade for the public MCP endpoint table (#2675)."""

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

PRIOR = "f26_2645_passkeys"
REVISION = "f27_2675_public_mcp_endpoints"


def _seed_workspace_context_resource(conn) -> tuple:
    workspace_id, context_id, resource_pk = uuid4(), uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, created_at) "
            "VALUES (:id, 'ws', 'owner-1', now())"
        ),
        {"id": workspace_id},
    )
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_at, is_private, is_public, "
            "resource_id) VALUES (:id, :ws, 'catalog', now(), false, true, 'catalog')"
        ),
        {"id": context_id, "ws": workspace_id},
    )
    conn.execute(
        text(
            "INSERT INTO resources (id, workspace_id, resource_id, created_at) "
            "VALUES (:id, :ws, 'catalog', now())"
        ),
        {"id": resource_pk, "ws": workspace_id},
    )
    return workspace_id, context_id, resource_pk


def test_f27_creates_public_mcp_endpoints_and_downgrades_cleanly():
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(PRIOR)
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            columns = dict(
                conn.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name='public_mcp_endpoints'"
                    )
                ).all()
            )
            assert columns == {
                "id": "uuid",
                "public_id": "character varying",
                "workspace_id": "uuid",
                "context_id": "uuid",
                "resource_pk": "uuid",
                "resource_id": "character varying",
                "public_name": "character varying",
                "public_description": "text",
                "public_usage_guide": "text",
                "created_by": "character varying",
                "created_at": "timestamp without time zone",
                "updated_at": "timestamp without time zone",
                "revoked_at": "timestamp without time zone",
                "revoked_by": "character varying",
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
                        "WHERE tc.table_name = 'public_mcp_endpoints'"
                    )
                ).all()
            }
            assert fks == {
                ("workspaces", "CASCADE"),
                ("contexts", "CASCADE"),
                ("resources", "CASCADE"),
            }
            indexes = {
                row.indexname: row.indexdef
                for row in conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'public_mcp_endpoints'"
                    )
                ).all()
            }
            assert "ix_public_mcp_endpoints_workspace_id" in indexes
            assert "public_mcp_endpoints_public_id_key" in indexes
            active = indexes["ux_public_mcp_endpoints_context_active"]
            assert "UNIQUE" in active
            assert "revoked_at IS NULL" in active

        with engine.begin() as conn:
            workspace_id, context_id, resource_pk = _seed_workspace_context_resource(conn)
            conn.execute(
                text(
                    "INSERT INTO public_mcp_endpoints (workspace_id, context_id, resource_pk, "
                    "resource_id, public_name, created_by, revoked_at) VALUES (:ws, :ctx, :res, "
                    "'catalog', 'Old', 'owner-1', now())"
                ),
                {"ws": workspace_id, "ctx": context_id, "res": resource_pk},
            )
            conn.execute(
                text(
                    "INSERT INTO public_mcp_endpoints (workspace_id, context_id, resource_pk, "
                    "resource_id, public_name, created_by) VALUES (:ws, :ctx, :res, "
                    "'catalog', 'Live', 'owner-1')"
                ),
                {"ws": workspace_id, "ctx": context_id, "res": resource_pk},
            )
            row = conn.execute(
                text(
                    "SELECT id, public_id, created_at FROM public_mcp_endpoints "
                    "WHERE public_name = 'Live'"
                )
            ).one()
            # The server defaults fill the identity, the opaque id and the time.
            assert row.id is not None
            assert row.public_id.startswith("pmcp_")
            assert len(row.public_id) == len("pmcp_") + 22
            assert row.created_at is not None

        # A second live endpoint for the same context is refused.
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO public_mcp_endpoints (workspace_id, context_id, resource_pk, "
                    "resource_id, public_name, created_by) VALUES (:ws, :ctx, :res, "
                    "'catalog', 'Second', 'owner-1')"
                ),
                {"ws": workspace_id, "ctx": context_id, "res": resource_pk},
            )

        _downgrade_to(PRIOR)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('public.public_mcp_endpoints')")).scalar()
                is None
            )
    finally:
        engine.dispose()
        _reset_alembic_state()
        _upgrade_to("head")
