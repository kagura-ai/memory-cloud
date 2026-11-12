"""Real PostgreSQL round trip and isolation for the first-party OIDC schema."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from models.auth import User
from models.oidc import OIDCAuthorizationCode, OIDCClient, OIDCGrant, OIDCToken
from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)
from utils.datetime import utcnow

PRIOR = "f35_1759_channel_routes"
REVISION = "f36_2781_oidc_provider"
TABLES = ("oidc_clients", "oidc_grants", "oidc_authorization_codes", "oidc_tokens")


def test_oidc_upgrade_constraints_erasure_and_downgrade() -> None:
    """New tables retain spent verifiers, cascade erasure and leave MCP data alone."""
    _reset_alembic_state()
    engine = _sync_engine()
    now = utcnow()
    grant_id, token_id = uuid4(), uuid4()
    client_id, user_id = "migration-oidc-client", "migration-oidc-subject"
    try:
        _upgrade_to(PRIOR)
        with engine.begin() as conn:
            conn.execute(insert(User).values(user_id=user_id, email="oidc-migration@example.test"))
            conn.execute(
                text(
                    "INSERT INTO oauth_clients "
                    "(client_id,client_secret_hash,client_name,redirect_uris,grant_types,"
                    "response_types,scope,token_endpoint_auth_method) VALUES "
                    "(:client,:hash,'Existing MCP client',CAST(:redirect AS json),"
                    "CAST(:grants AS json),CAST(:responses AS json),'memory:read',"
                    "'client_secret_post')"
                ),
                {
                    "client": client_id,
                    "hash": "0" * 64,
                    "redirect": '["https://mcp.example.test/callback"]',
                    "grants": '["authorization_code","refresh_token"]',
                    "responses": '["code"]',
                },
            )
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            inspector = inspect(conn)
            for model in (OIDCClient, OIDCGrant, OIDCAuthorizationCode, OIDCToken):
                name = model.__tablename__
                assert inspector.has_table(name)
                columns = {col["name"]: col for col in inspector.get_columns(name)}
                assert set(columns) == set(model.__table__.columns.keys())
                assert inspector.get_pk_constraint(name)["constrained_columns"] == [
                    col.name for col in model.__table__.primary_key
                ]
                expected_indexes = {index.name for index in model.__table__.indexes}
                assert {index["name"] for index in inspector.get_indexes(name)} >= expected_indexes
                for foreign_key in inspector.get_foreign_keys(name):
                    assert foreign_key["options"]["ondelete"] == "CASCADE"
            redirect_column = next(
                col
                for col in inspector.get_columns("oidc_clients")
                if col["name"] == "redirect_uris"
            )
            assert isinstance(redirect_column["type"], JSONB)
            grant_foreign_tables = {
                key["referred_table"] for key in inspector.get_foreign_keys("oidc_grants")
            }
            assert grant_foreign_tables == {"oidc_clients", "users"}
            for name in ("oidc_authorization_codes", "oidc_tokens"):
                assert {key["referred_table"] for key in inspector.get_foreign_keys(name)} == {
                    "oidc_grants"
                }
            assert "nonce" in {
                column
                for constraint in inspector.get_unique_constraints("oidc_grants")
                for column in constraint["column_names"]
            }
            code_columns = {
                col["name"] for col in inspector.get_columns("oidc_authorization_codes")
            }
            token_columns = {col["name"] for col in inspector.get_columns("oidc_tokens")}
            assert "code" not in code_columns
            assert not {"access_token", "refresh_token", "id_token"} & token_columns
        with engine.begin() as conn:
            conn.execute(
                insert(OIDCClient).values(
                    client_id=client_id,
                    client_name="Isolated OIDC client",
                    client_secret_hash="$2b$04$not-a-plaintext-secret",
                    redirect_uris=["https://dashboard.example.test/auth/callback"],
                    scope="openid profile",
                )
            )
            conn.execute(
                insert(OIDCGrant).values(
                    id=grant_id,
                    client_id=client_id,
                    user_id=user_id,
                    scope="openid profile",
                    nonce="migration-nonce",
                    auth_time=1,
                    created_at=now,
                    expires_at=now + timedelta(hours=8),
                )
            )
            conn.execute(
                insert(OIDCAuthorizationCode).values(
                    code_hash="a" * 64,
                    grant_id=grant_id,
                    redirect_uri="https://dashboard.example.test/auth/callback",
                    code_challenge="x" * 43,
                    code_challenge_method="S256",
                    expires_at=now + timedelta(seconds=60),
                    consumed_at=now,
                )
            )
            conn.execute(
                insert(OIDCToken).values(
                    id=token_id,
                    grant_id=grant_id,
                    scope="openid profile",
                    access_token_hash="b" * 64,
                    refresh_token_hash="c" * 64,
                    issued_at=1,
                    expires_in=300,
                    refresh_used_at=now,
                )
            )
        # A reused client/nonce must not race a second successful issuance.
        with engine.begin() as conn:
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    insert(OIDCGrant).values(
                        id=uuid4(),
                        client_id=client_id,
                        user_id=user_id,
                        scope="openid",
                        nonce="migration-nonce",
                        auth_time=1,
                        created_at=now,
                        expires_at=now + timedelta(hours=8),
                    )
                )
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    insert(OIDCToken).values(
                        id=uuid4(),
                        grant_id=grant_id,
                        scope="openid",
                        access_token_hash="d" * 64,
                        refresh_token_hash="c" * 64,
                        issued_at=1,
                        expires_in=300,
                    )
                )
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    insert(OIDCAuthorizationCode).values(
                        code_hash="d" * 64,
                        grant_id=grant_id,
                        redirect_uri="https://dashboard.example.test/auth/callback",
                        code_challenge="x" * 43,
                        code_challenge_method="plain",
                        expires_at=now + timedelta(seconds=60),
                    )
                )
        with engine.connect() as conn:
            assert conn.scalar(select(OIDCAuthorizationCode.code_hash)) == "a" * 64
            assert conn.scalar(select(OIDCToken.refresh_token_hash)) == "c" * 64
        with engine.begin() as conn:
            conn.execute(User.__table__.delete().where(User.user_id == user_id))
            for model in (OIDCGrant, OIDCAuthorizationCode, OIDCToken):
                assert not conn.execute(select(model.__table__)).all()
            assert conn.scalar(select(OIDCClient.client_id)) == client_id
            assert (
                conn.scalar(
                    text("SELECT scope FROM oauth_clients WHERE client_id = :id"), {"id": client_id}
                )
                == "memory:read"
            )
        _downgrade_to(PRIOR)
        with engine.connect() as conn:
            inspector = inspect(conn)
            assert not any(inspector.has_table(name) for name in TABLES)
            assert (
                conn.scalar(
                    text("SELECT scope FROM oauth_clients WHERE client_id = :id"), {"id": client_id}
                )
                == "memory:read"
            )
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert all(inspect(conn).has_table(name) for name in TABLES)
    finally:
        try:
            _upgrade_to("head")
        finally:
            engine.dispose()
