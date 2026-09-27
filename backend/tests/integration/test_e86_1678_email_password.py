"""DB contract for e86 (#1678): ``users.email_verified_at`` + ``email_action_tokens``.

- upgrade backfills ``email_verified_at`` for users with a linked OAuth
  provider, never for ``@local`` addresses and never for password-only users;
- ``email_action_tokens`` has the ``purpose`` CHECK, the unique ``token_hash``,
  the ``(user_id, purpose)`` index, and cascades with its user;
- downgrade drops the table and the column; the round trip is repeatable.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PRE_E86_REV = "e85_1665_terms_acceptances"
E86_REV = "e86_1678_email_password"


def _seed_user(conn: Connection, *, email: str | None = None, provider: str | None = None) -> str:
    user_id = f"u-{uuid.uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO users (email, user_id, role, timezone, locale, is_initial_admin)"
            " VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": email or f"{user_id}@test.example", "uid": user_id},
    )
    if provider:
        conn.execute(
            text(
                "INSERT INTO user_oauth_providers (user_id, provider, oauth_sub)"
                " VALUES (:uid, :provider, :sub)"
            ),
            {"uid": user_id, "provider": provider, "sub": f"sub-{user_id}"},
        )
    return user_id


def _verified_at(conn: Connection, user_id: str):
    return conn.execute(
        text("SELECT email_verified_at FROM users WHERE user_id = :uid"), {"uid": user_id}
    ).scalar_one()


def _insert_token(
    conn: Connection, *, user_id: str, purpose: str = "reset_password", token_hash: str = "a" * 64
) -> None:
    conn.execute(
        text(
            "INSERT INTO email_action_tokens (user_id, purpose, token_hash, email, expires_at)"
            " VALUES (:uid, :purpose, :hash, 'x@test.example', now() + interval '30 minutes')"
        ),
        {"uid": user_id, "purpose": purpose, "hash": token_hash},
    )


def _table_exists(conn: Connection) -> bool:
    return (
        conn.execute(text("SELECT to_regclass('public.email_action_tokens')")).scalar_one()
        is not None
    )


def _column_exists(conn: Connection) -> bool:
    return (
        conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'users' AND column_name = 'email_verified_at'"
            )
        ).scalar_one()
        == 1
    )


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE86EmailPassword:
    def test_backfill_table_and_round_trip(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E86_REV)
            engine = _sync_engine()

            with engine.begin() as conn:
                assert not _table_exists(conn)
                assert not _column_exists(conn)
                google_user = _seed_user(conn, provider="google")
                github_user = _seed_user(conn, provider="github")
                password_only = _seed_user(conn)
                cli_admin = _seed_user(conn, email="admin@local", provider="google")
                cli_admin_upper = _seed_user(conn, email="Root@LOCAL", provider="github")

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E86_REV)

            with engine.begin() as conn:
                assert _table_exists(conn)
                # IdP-verified addresses are marked; the rest are not.
                assert _verified_at(conn, google_user) is not None
                assert _verified_at(conn, github_user) is not None
                assert _verified_at(conn, password_only) is None
                assert _verified_at(conn, cli_admin) is None
                assert _verified_at(conn, cli_admin_upper) is None
                # Naive UTC column.
                data_type = conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns"
                        " WHERE table_name = 'users' AND column_name = 'email_verified_at'"
                    )
                ).scalar_one()
                assert data_type == "timestamp without time zone"

                index = conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes"
                        " WHERE indexname = 'ix_email_action_tokens_user_purpose'"
                    )
                ).scalar_one()
                assert "(user_id, purpose)" in index
                _insert_token(conn, user_id=password_only)

            # The purpose vocabulary is closed.
            with pytest.raises(IntegrityError, match="valid_email_action_purpose"):
                with engine.begin() as conn:
                    _insert_token(conn, user_id=password_only, purpose="login", token_hash="b" * 64)

            # A digest is stored once.
            with pytest.raises(IntegrityError, match="email_action_tokens_token_hash_key"):
                with engine.begin() as conn:
                    _insert_token(conn, user_id=google_user)

            # Tokens go with their user.
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": password_only})
                remaining = conn.execute(
                    text("SELECT count(*) FROM email_action_tokens WHERE user_id = :uid"),
                    {"uid": password_only},
                ).scalar_one()
                assert remaining == 0

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E86_REV)

            with engine.begin() as conn:
                assert not _table_exists(conn)
                assert not _column_exists(conn)

            # Repeatable.
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E86_REV)
            with engine.begin() as conn:
                assert _table_exists(conn)
                assert _verified_at(conn, google_user) is not None
                for uid in (google_user, github_user, cli_admin, cli_admin_upper):
                    conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": uid})
        finally:
            _leave_db_at_head()
