"""Real Alembic upgrade/downgrade for the observation credential table (#2679)."""

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

PRIOR = "f27_2675_public_mcp_endpoints"
REVISION = "f29_2679_observation_credentials"

_INSERT = (
    "INSERT INTO observation_credentials (key_hash, key_prefix, name, workspace_id, "
    "resource_pk, scope, created_by, expires_at) VALUES (:hash, 'kagura_obs_abcde', 'n', "
    ":ws, :res, :scope, 'owner-1', now() + interval '1 day') RETURNING id, public_id"
)


def _seed(conn) -> tuple:
    workspace_id, resource_pk = uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, created_at) "
            "VALUES (:id, 'ws', 'owner-1', now())"
        ),
        {"id": workspace_id},
    )
    conn.execute(
        text(
            "INSERT INTO resources (id, workspace_id, resource_id, created_at) "
            "VALUES (:id, :ws, 'catalog', now())"
        ),
        {"id": resource_pk, "ws": workspace_id},
    )
    return workspace_id, resource_pk


def test_f29_creates_observation_credentials_and_downgrades_cleanly():
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
                        "WHERE table_name='observation_credentials'"
                    )
                ).all()
            )
            assert columns == {
                "id": "integer",
                "public_id": "character varying",
                "key_hash": "character varying",
                "key_prefix": "character varying",
                "name": "character varying",
                "workspace_id": "uuid",
                "resource_pk": "uuid",
                "scope": "character varying",
                "created_by": "character varying",
                "created_at": "timestamp without time zone",
                "expires_at": "timestamp without time zone",
                "last_used_at": "timestamp without time zone",
                "revoked_at": "timestamp without time zone",
                "rotated_from_id": "integer",
            }
            nullable = dict(
                conn.execute(
                    text(
                        "SELECT column_name, is_nullable FROM information_schema.columns "
                        "WHERE table_name='observation_credentials'"
                    )
                ).all()
            )
            assert nullable["expires_at"] == "NO"
            assert nullable["resource_pk"] == "YES"
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
                        "WHERE tc.table_name = 'observation_credentials'"
                    )
                ).all()
            }
            assert fks == {
                ("workspaces", "CASCADE"),
                ("resources", "CASCADE"),
                ("observation_credentials", "SET NULL"),
            }
            indexes = {
                row.indexname: row.indexdef
                for row in conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'observation_credentials'"
                    )
                ).all()
            }
            for name in (
                "ix_observation_credentials_workspace_id",
                "ix_observation_credentials_expires_at",
                "ix_observation_credentials_revoked_at",
                "observation_credentials_public_id_key",
            ):
                assert name in indexes
            assert "UNIQUE" in indexes["observation_credentials_key_hash_key"]

        with engine.begin() as conn:
            workspace_id, resource_pk = _seed(conn)
            ws_row = conn.execute(
                text(_INSERT),
                {"hash": "a" * 64, "ws": workspace_id, "res": None, "scope": "workspace"},
            ).one()
            assert ws_row.public_id.startswith("obsk_")
            assert len(ws_row.public_id) == len("obsk_") + 22
            conn.execute(
                text(_INSERT),
                {"hash": "b" * 64, "ws": workspace_id, "res": resource_pk, "scope": "resource"},
            )

        # The verifier is unique.
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(_INSERT),
                {"hash": "a" * 64, "ws": workspace_id, "res": None, "scope": "workspace"},
            )
        # A resource scope needs its resource; a workspace scope must not carry one.
        for scope, res in (("resource", None), ("workspace", resource_pk), ("deployment", None)):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    text(_INSERT),
                    {"hash": uuid4().hex * 2, "ws": workspace_id, "res": res, "scope": scope},
                )
        # The expiry is mandatory.
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO observation_credentials (key_hash, key_prefix, name, "
                    "workspace_id, scope, created_by) VALUES (:hash, 'p', 'n', :ws, "
                    "'workspace', 'owner-1')"
                ),
                {"hash": "c" * 64, "ws": workspace_id},
            )

        _downgrade_to(PRIOR)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('public.observation_credentials')")).scalar()
                is None
            )
    finally:
        engine.dispose()
        _reset_alembic_state()
        _upgrade_to("head")
