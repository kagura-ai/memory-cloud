"""Migration round-trip for f09_2129_cred_default_ctx (#2129).

1. upgrade adds the nullable ``api_keys.default_context_id`` (uuid, indexed,
   ``ON DELETE SET NULL``) and its two CHECKs — no default on a public-bound
   key, none on an agent-bound key — and the ``oauth_grant_settings`` table
   (PK ``(user_id, client_id)``; the row goes with its client, its default
   goes with its context);
2. downgrade drops the table, the CHECKs, the index and the column again.

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

F09_REVISION = "f09_2129_cred_default_ctx"
PRIOR_HEAD = "f08_2107_chunk_gap"
OWNER = "f09-migration-test"


def _key_column(conn) -> tuple[str, str] | None:
    row = conn.execute(
        text(
            "SELECT is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name = 'api_keys' AND column_name = 'default_context_id'"
        )
    ).first()
    return (row[0], row[1]) if row else None


def _constraints(conn) -> set[str]:
    return set(
        conn.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conname IN "
                "('ck_api_keys_default_not_bound', 'ck_api_keys_default_not_agent')"
            )
        ).scalars()
    )


def _table_exists(conn) -> bool:
    return conn.execute(text("SELECT to_regclass('oauth_grant_settings') IS NOT NULL")).scalar_one()


def _insert_key(conn, suffix: str, **cols) -> None:
    values = {
        "hash": f"f09_{suffix}".ljust(64, "0"),
        "prefix": f"kagura_{suffix}"[:16],
        "name": f"f09-{suffix}",
        "uid": OWNER,
        "ws": None,
        "bound": None,
        "agent": None,
        "dflt": None,
        **cols,
    }
    conn.execute(
        text(
            "INSERT INTO api_keys (key_hash, key_prefix, name, user_id, workspace_id, "
            "bound_context_id, agent_id, default_context_id) "
            "VALUES (:hash, :prefix, :name, :uid, :ws, :bound, :agent, :dflt)"
        ),
        values,
    )


def test_f09_round_trip() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    workspace_id, ctx_id, gone_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    agent_id = uuid.uuid4()
    client_id = f"f09-{uuid.uuid4().hex[:12]}"
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _key_column(conn) is None
            assert not _table_exists(conn)

        _upgrade_to(F09_REVISION)
        with engine.connect() as conn:
            assert _key_column(conn) == ("YES", "uuid")
            assert _constraints(conn) == {
                "ck_api_keys_default_not_bound",
                "ck_api_keys_default_not_agent",
            }
            assert conn.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = 'idx_api_keys_default_context_id'")
            ).scalar_one_or_none()
            assert _table_exists(conn)

            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, :owner, 'free')"
                ),
                {"id": workspace_id, "name": f"f09-{workspace_id.hex[:8]}", "owner": OWNER},
            )
            for cid in (ctx_id, gone_id):
                conn.execute(
                    text(
                        "INSERT INTO contexts (id, workspace_id, name, created_by, is_public) "
                        "VALUES (:id, :ws, :name, :owner, true)"
                    ),
                    {"id": cid, "ws": workspace_id, "name": f"f09-{cid.hex[:8]}", "owner": OWNER},
                )
            conn.execute(
                text(
                    "INSERT INTO agents (id, workspace_id, name, owner_user_id) "
                    "VALUES (:id, :ws, 'f09-agent', :owner)"
                ),
                {"id": agent_id, "ws": workspace_id, "owner": OWNER},
            )
            conn.execute(
                text(
                    "INSERT INTO oauth_clients (client_id, client_secret_hash, client_name, "
                    "redirect_uris, grant_types, response_types, scope, "
                    "token_endpoint_auth_method) VALUES (:cid, 'x', 'f09 app', "
                    "'[\"http://127.0.0.1/cb\"]', '[\"authorization_code\"]', '[\"code\"]', "
                    "'memory:read', 'none')"
                ),
                {"cid": client_id},
            )
            _insert_key(conn, "plain", dflt=gone_id)
            conn.execute(
                text(
                    "INSERT INTO oauth_grant_settings (user_id, client_id, default_context_id) "
                    "VALUES (:uid, :cid, :ctx)"
                ),
                {"uid": OWNER, "cid": client_id, "ctx": gone_id},
            )
            conn.commit()

            # The CHECKs: no default on a public-bound or an agent-bound key.
            with pytest.raises(IntegrityError, match="ck_api_keys_default_not_bound"):
                _insert_key(conn, "bound", bound=ctx_id, dflt=ctx_id)
            conn.rollback()
            with pytest.raises(IntegrityError, match="ck_api_keys_default_not_agent"):
                _insert_key(conn, "agent", ws=workspace_id, agent=agent_id, dflt=ctx_id)
            conn.rollback()

            # ON DELETE SET NULL: the context goes, both rows stay without a default.
            conn.execute(text("DELETE FROM contexts WHERE id = :id"), {"id": gone_id})
            conn.commit()
            assert (
                conn.execute(
                    text("SELECT default_context_id FROM api_keys WHERE name = 'f09-plain'")
                ).scalar_one()
                is None
            )
            assert (
                conn.execute(
                    text(
                        "SELECT default_context_id FROM oauth_grant_settings "
                        "WHERE user_id = :uid AND client_id = :cid"
                    ),
                    {"uid": OWNER, "cid": client_id},
                ).scalar_one()
                is None
            )

            # The primary key: one row per (user, client).
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO oauth_grant_settings (user_id, client_id) VALUES (:uid, :cid)"
                    ),
                    {"uid": OWNER, "cid": client_id},
                )
            conn.rollback()

            # ON DELETE CASCADE: the client goes, its settings rows go with it.
            conn.execute(
                text("DELETE FROM oauth_clients WHERE client_id = :cid"), {"cid": client_id}
            )
            conn.commit()
            assert (
                conn.execute(
                    text("SELECT count(*) FROM oauth_grant_settings WHERE client_id = :cid"),
                    {"cid": client_id},
                ).scalar_one()
                == 0
            )
            conn.execute(text("DELETE FROM api_keys WHERE user_id = :uid"), {"uid": OWNER})
            conn.commit()

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _key_column(conn) is None
            assert _constraints(conn) == set()
            assert not _table_exists(conn)
    finally:
        try:
            _upgrade_to("head")
            with engine.connect() as conn:
                conn.execute(text("DELETE FROM api_keys WHERE user_id = :uid"), {"uid": OWNER})
                conn.execute(
                    text("DELETE FROM agents WHERE workspace_id = :ws"), {"ws": workspace_id}
                )
                conn.execute(
                    text("DELETE FROM contexts WHERE workspace_id = :ws"), {"ws": workspace_id}
                )
                conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
                conn.commit()
        finally:
            engine.dispose()
