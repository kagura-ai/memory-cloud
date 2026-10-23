"""Migration round-trip for f13_1952_api_keys_connector_id (#1952).

1. upgrade adds the nullable ``api_keys.connector_id`` (uuid,
   ``fk_api_keys_connector_id`` → ``workspace_connectors`` ``ON DELETE
   CASCADE``), its partial index and three CHECKs, and links every key of the
   same workspace whose name is exactly ``connector:`` + a connector id in the
   canonical spelling — revoked rows included. Look-alikes (another
   workspace's connector, junk, a non-canonical spelling) and rows the CHECKs
   would refuse (agent-bound) stay NULL;
2. deleting the connector's ``resources`` row removes its linked keys through
   the cascade and leaves the rest;
3. downgrade drops the column, the CHECKs and the index, names intact;
4. re-upgrade links the same rows again (idempotent backfill).

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

F13_REVISION = "f13_1952_api_keys_connector_id"
PRIOR_HEAD = "f12_2244_rev_writer_erasure"
OWNER = "f13-migration-test"
CHECKS = {
    "ck_api_keys_connector_requires_workspace",
    "ck_api_keys_connector_not_agent",
    "ck_api_keys_connector_not_default",
}


def _column(conn: Connection) -> tuple[str, str] | None:
    row = conn.execute(
        text(
            "SELECT is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name = 'api_keys' AND column_name = 'connector_id'"
        )
    ).first()
    return (row[0], row[1]) if row else None


def _constraints(conn: Connection) -> dict[str, bool]:
    """``{conname: convalidated}`` for the f13 constraints present."""
    rows = conn.execute(
        text(
            "SELECT conname, convalidated FROM pg_constraint "
            "WHERE conrelid = 'api_keys'::regclass AND (conname = ANY(:names) "
            "OR conname = 'fk_api_keys_connector_id')"
        ),
        {"names": list(CHECKS)},
    ).all()
    return {row[0]: row[1] for row in rows}


def _index_valid(conn: Connection) -> bool | None:
    return conn.execute(
        text(
            "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
            "WHERE c.relname = 'idx_api_keys_connector'"
        )
    ).scalar_one_or_none()


def _workspace(conn: Connection) -> uuid.UUID:
    workspace_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :owner, 'free')"
        ),
        {"id": workspace_id, "name": f"f13-{workspace_id.hex[:8]}", "owner": OWNER},
    )
    return workspace_id


def _connector(conn: Connection, workspace_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A ``resources`` row and its connector; returns ``(connector_id, resource_pk)``."""
    resource_pk, connector_id = uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text("INSERT INTO resources (id, workspace_id, resource_id) VALUES (:id, :ws, :rid)"),
        {"id": resource_pk, "ws": workspace_id, "rid": f"f13-{resource_pk.hex[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO workspace_connectors (id, resource_pk, workspace_id, connector_type, "
            "created_by) VALUES (:id, :rpk, :ws, 'slack', :owner)"
        ),
        {"id": connector_id, "rpk": resource_pk, "ws": workspace_id, "owner": OWNER},
    )
    return connector_id, resource_pk


def _insert_key(conn: Connection, suffix: str, name: str, **cols: object) -> None:
    values = {
        "hash": f"f13_{suffix}".ljust(64, "0"),
        "prefix": f"kagura_{suffix}"[:16],
        "name": name,
        "uid": OWNER,
        "ws": None,
        "agent": None,
        "dflt": None,
        "revoked": None,
        **cols,
    }
    conn.execute(
        text(
            "INSERT INTO api_keys (key_hash, key_prefix, name, user_id, workspace_id, "
            "agent_id, default_context_id, revoked_at) "
            "VALUES (:hash, :prefix, :name, :uid, :ws, :agent, :dflt, :revoked)"
        ),
        values,
    )


def _links(conn: Connection) -> dict[str, uuid.UUID | None]:
    rows = conn.execute(
        text("SELECT key_hash, connector_id FROM api_keys WHERE user_id = :uid"), {"uid": OWNER}
    ).all()
    return {row[0][4:].rstrip("0"): row[1] for row in rows}


def test_f13_round_trip() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _column(conn) is None
            ws, other_ws = _workspace(conn), _workspace(conn)
            connector_id, resource_pk = _connector(conn, ws)
            other_connector_id, _ = _connector(conn, other_ws)
            agent_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO agents (id, workspace_id, name, owner_user_id) "
                    "VALUES (:id, :ws, 'f13-agent', :owner)"
                ),
                {"id": agent_id, "ws": ws, "owner": OWNER},
            )
            name = f"connector:{connector_id}"
            # (a) the connector's own key, active.
            _insert_key(conn, "a", name, ws=ws)
            # (b) the same name, revoked: history links too.
            _insert_key(conn, "b", name, ws=ws, revoked="2026-01-01")
            # (c) a key in ws naming the other workspace's connector.
            _insert_key(conn, "c", f"connector:{other_connector_id}", ws=ws)
            # (d) junk suffix — compared as text, never cast, so no error.
            _insert_key(conn, "d", "connector:junk", ws=ws)
            # (e) a non-canonical spelling of a real id.
            _insert_key(conn, "e", f"connector:{str(connector_id).upper()}", ws=ws)
            # (f) the real name on an agent-bound key (pre-#1953 shape):
            # ck_api_keys_connector_not_agent would refuse the link.
            _insert_key(conn, "f", name, ws=ws, agent=agent_id)
            # (g) an ordinary key.
            _insert_key(conn, "g", "laptop", ws=ws)
            conn.commit()

        _upgrade_to(F13_REVISION)
        with engine.connect() as conn:
            assert _column(conn) == ("YES", "uuid")
            assert _constraints(conn) == dict.fromkeys(CHECKS | {"fk_api_keys_connector_id"}, True)
            assert (
                conn.execute(
                    text(
                        "SELECT confdeltype FROM pg_constraint "
                        "WHERE conname = 'fk_api_keys_connector_id'"
                    )
                ).scalar_one()
                == "c"
            )
            assert _index_valid(conn) is True
            assert _links(conn) == {
                "a": connector_id,
                "b": connector_id,
                "c": None,
                "d": None,
                "e": None,
                "f": None,
                "g": None,
            }

            # The CHECKs.
            with pytest.raises(IntegrityError, match="ck_api_keys_connector_requires_workspace"):
                conn.execute(
                    text(
                        "INSERT INTO api_keys (key_hash, key_prefix, name, user_id, "
                        "connector_id) VALUES (:h, 'kagura_x', 'x', :uid, :cid)"
                    ),
                    {"h": "f13_ck_ws".ljust(64, "0"), "uid": OWNER, "cid": connector_id},
                )
            conn.rollback()
            with pytest.raises(IntegrityError, match="ck_api_keys_connector_not_agent"):
                conn.execute(
                    text(
                        "INSERT INTO api_keys (key_hash, key_prefix, name, user_id, "
                        "workspace_id, agent_id, connector_id) "
                        "VALUES (:h, 'kagura_x', 'x', :uid, :ws, :agent, :cid)"
                    ),
                    {
                        "h": "f13_ck_agent".ljust(64, "0"),
                        "uid": OWNER,
                        "ws": ws,
                        "agent": agent_id,
                        "cid": connector_id,
                    },
                )
            conn.rollback()
            ctx_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO contexts (id, workspace_id, name, created_by) "
                    "VALUES (:id, :ws, 'f13-ctx', :owner)"
                ),
                {"id": ctx_id, "ws": ws, "owner": OWNER},
            )
            with pytest.raises(IntegrityError, match="ck_api_keys_connector_not_default"):
                conn.execute(
                    text(
                        "INSERT INTO api_keys (key_hash, key_prefix, name, user_id, "
                        "workspace_id, default_context_id, connector_id) "
                        "VALUES (:h, 'kagura_x', 'x', :uid, :ws, :ctx, :cid)"
                    ),
                    {
                        "h": "f13_ck_dflt".ljust(64, "0"),
                        "uid": OWNER,
                        "ws": ws,
                        "ctx": ctx_id,
                        "cid": connector_id,
                    },
                )
            conn.rollback()

        # Downgrade: everything f13 added goes; the names stay.
        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _column(conn) is None
            assert _constraints(conn) == {}
            assert _index_valid(conn) is None
            assert (
                conn.execute(
                    text("SELECT count(*) FROM api_keys WHERE user_id = :uid AND name = :name"),
                    {"uid": OWNER, "name": name},
                ).scalar_one()
                == 3
            )

        # Re-upgrade: the backfill links exactly the same rows.
        _upgrade_to(F13_REVISION)
        with engine.connect() as conn:
            assert _links(conn) == {
                "a": connector_id,
                "b": connector_id,
                "c": None,
                "d": None,
                "e": None,
                "f": None,
                "g": None,
            }

            # ON DELETE CASCADE: the connector's resource goes (the path that
            # bypasses delete_connector), its linked keys go with it.
            conn.execute(text("DELETE FROM resources WHERE id = :id"), {"id": resource_pk})
            conn.commit()
            assert set(_links(conn)) == {"c", "d", "e", "f", "g"}
    finally:
        try:
            _upgrade_to("head")
            with engine.connect() as conn:
                conn.execute(text("DELETE FROM api_keys WHERE user_id = :uid"), {"uid": OWNER})
                conn.execute(
                    text("DELETE FROM workspaces WHERE owner_user_id = :uid"), {"uid": OWNER}
                )
                conn.commit()
        finally:
            engine.dispose()
