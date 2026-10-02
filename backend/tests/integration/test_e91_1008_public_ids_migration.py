"""DB contract for the public-id migration e91 (#1008)."""

import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PRE_E91_REV = "e90_1784_identity_links"
E91_REV = "e91_1008_public_ids"

PREFIXES = {
    "api_keys": "akey",
    "share_keys": "skey",
    "resource_tokens": "rtok",
    "workspace_invitations": "winv",
}


def _seed(conn: Connection) -> dict[str, list[int]]:
    """Insert active, revoked and expired rows into the four tables."""
    suffix = uuid.uuid4().hex[:12]
    user_id = f"u-{suffix}"
    workspace_id = str(uuid.uuid4())
    context_id = str(uuid.uuid4())
    now = datetime(2026, 1, 1)
    past = now - timedelta(days=30)
    conn.execute(
        text(
            "INSERT INTO users (email, user_id, role, timezone, locale, is_initial_admin) "
            "VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{user_id}@test.example", "uid": user_id},
    )
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": workspace_id, "name": f"ws-{suffix}", "owner": user_id},
    )
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) "
            "VALUES (:id, :ws, :name, :owner)"
        ),
        {"id": context_id, "ws": workspace_id, "name": f"ctx-{suffix}", "owner": user_id},
    )
    ids: dict[str, list[int]] = {t: [] for t in PREFIXES}
    for i, (revoked, expires) in enumerate([(None, None), (past, None), (None, past)]):
        ids["api_keys"].append(
            conn.execute(
                text(
                    "INSERT INTO api_keys "
                    "(key_hash, key_prefix, name, user_id, revoked_at, expires_at) "
                    "VALUES (:hash, 'kagura_xxxxxxxxx', :name, :uid, :rev, :exp) RETURNING id"
                ),
                {
                    "hash": f"{suffix}{i}".ljust(64, "0"),
                    "name": f"k-{i}",
                    "uid": user_id,
                    "rev": revoked,
                    "exp": expires,
                },
            ).scalar_one()
        )
        ids["share_keys"].append(
            conn.execute(
                text(
                    "INSERT INTO share_keys "
                    "(key_hash, key_prefix, name, user_id, context_id, revoked_at, expires_at) "
                    "VALUES (:hash, 'kagura_xxxxxxxxx', :name, :uid, :ctx, :rev, :exp) "
                    "RETURNING id"
                ),
                {
                    "hash": f"s{suffix}{i}".ljust(64, "0"),
                    "name": f"s-{i}",
                    "uid": user_id,
                    "ctx": context_id,
                    "rev": revoked,
                    "exp": expires or now + timedelta(days=1),
                },
            ).scalar_one()
        )
        ids["resource_tokens"].append(
            conn.execute(
                text(
                    "INSERT INTO resource_tokens "
                    "(resource_id, workspace_id, token_hash, quota_events_per_hour, is_active) "
                    "VALUES (:rid, :ws, :hash, 1000, :active) RETURNING id"
                ),
                {
                    "rid": f"r-{suffix}",
                    "ws": workspace_id,
                    "hash": f"t{suffix}{i}",
                    "active": revoked is None,
                },
            ).scalar_one()
        )
        ids["workspace_invitations"].append(
            conn.execute(
                text(
                    "INSERT INTO workspace_invitations "
                    "(workspace_id, token, email, invited_by, expires_at, accepted_at) "
                    "VALUES (:ws, :token, :email, :uid, :exp, :acc) RETURNING id"
                ),
                {
                    "ws": workspace_id,
                    "token": f"invite-token-{suffix}-{i}".ljust(24, "x"),
                    "email": f"invitee{i}-{suffix}@test.example",
                    "uid": user_id,
                    "exp": expires,
                    "acc": revoked,
                },
            ).scalar_one()
        )
    return ids


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


def _column_exists(conn: Connection, table: str) -> bool:
    return (
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'public_id'"
            ),
            {"t": table},
        ).first()
        is not None
    )


class TestE91PublicIdsMigration:
    def test_backfill_constraints_server_default_and_downgrade(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E91_REV)
            engine = _sync_engine()
            with engine.begin() as conn:
                seeded = _seed(conn)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E91_REV)

            with engine.begin() as conn:
                for table, prefix in PREFIXES.items():
                    rows = conn.execute(
                        text(f"SELECT id, public_id FROM {table} WHERE id = ANY(:ids)"),  # noqa: S608
                        {"ids": seeded[table]},
                    ).all()
                    # Every row — revoked and expired included — got an id.
                    assert len(rows) == 3, table
                    pids = [r.public_id for r in rows]
                    for pid in pids:
                        assert re.fullmatch(rf"{prefix}_[0-9A-Za-z]{{22}}", pid), (table, pid)
                    assert len(set(pids)) == 3, table

                    col = conn.execute(
                        text(
                            "SELECT is_nullable, column_default FROM information_schema.columns "
                            "WHERE table_name = :t AND column_name = 'public_id'"
                        ),
                        {"t": table},
                    ).one()
                    assert col.is_nullable == "NO", table
                    assert col.column_default is not None and "gen_random_uuid" in (
                        col.column_default
                    ), table

                    uq = conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.table_constraints "
                            "WHERE table_name = :t AND constraint_name = :n "
                            "AND constraint_type = 'UNIQUE'"
                        ),
                        {"t": table, "n": f"{table}_public_id_key"},
                    ).first()
                    assert uq is not None, table

                # A pre-#1008 app instance inserts without public_id (rolling
                # deploy): the server default fills it in, matching the pattern.
                pid = conn.execute(
                    text(
                        "INSERT INTO api_keys (key_hash, key_prefix, name, user_id) "
                        "VALUES (:h, 'kagura_xxxxxxxxx', 'old-app', 'old-app-user') "
                        "RETURNING public_id"
                    ),
                    {"h": uuid.uuid4().hex.ljust(64, "0")},
                ).scalar_one()
                assert re.fullmatch(r"akey_[0-9A-Za-z]{22}", pid)

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E91_REV)
            with engine.begin() as conn:
                for table in PREFIXES:
                    assert not _column_exists(conn, table), table
        finally:
            _leave_db_at_head()
