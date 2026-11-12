"""Migration round trip for f37_1734_email_signup (#1734).

1. upgrade adds ``users.provider_auto_link_blocked_at``, widens the
   ``signup_allowlist`` provider CHECK to ``email`` (validated) and creates
   ``email_signup_requests`` with its unique token hash and two indexes;
2. the downgrade is refused while an allowlist row has ``provider='email'``
   or an account carries ``provider_auto_link_blocked_at`` (the owner's
   choice would be lost), and runs once neither holds (restoring the
   two-provider CHECK);
3. re-upgrade works (the steps before the VALIDATE commit are re-runnable).

PRIOR is read from the module, so re-chaining the revision changes one line.
Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import importlib
import sys
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.migration_ast import ALEMBIC_VERSIONS

if str(ALEMBIC_VERSIONS) not in sys.path:
    sys.path.insert(0, str(ALEMBIC_VERSIONS))

_MODULE = importlib.import_module("f37_1734_email_signup")


def test_f37_round_trip_and_the_guarded_downgrade() -> None:
    from tests.integration.test_alembic_migrations import (
        _downgrade_to,
        _reset_alembic_state,
        _sync_engine,
        _upgrade_to,
    )

    prior = _MODULE.down_revision
    _reset_alembic_state()
    engine = _sync_engine()

    def table_exists(conn) -> bool:
        return (
            conn.execute(text("SELECT to_regclass('email_signup_requests')")).scalar() is not None
        )

    def column_exists(conn) -> bool:
        return (
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_name = 'users' "
                    "AND column_name = 'provider_auto_link_blocked_at'"
                )
            ).first()
            is not None
        )

    def provider_check(conn) -> tuple[str, bool]:
        row = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
                "WHERE conname = 'valid_signup_allowlist_provider'"
            )
        ).first()
        assert row is not None
        return row[0], row[1]

    def insert_email_row(conn, subject: str) -> None:
        conn.execute(
            text(
                "INSERT INTO signup_allowlist (provider, subject_id, subject_label, "
                "github_user_id, github_username, source, state) "
                "VALUES ('email', :s, 'a@example.com', :g, 'a@example.com', "
                "'beta_invite', 'active')"
            ),
            {"s": subject, "g": f"email:{subject}"[:64]},
        )

    subject = f"email:{uuid4()}"
    blocked_user = f"f37-blocked-{uuid4().hex[:12]}"
    try:
        _upgrade_to(prior)
        with engine.connect() as conn:
            assert not table_exists(conn)
            assert not column_exists(conn)
            definition, _ = provider_check(conn)
            assert "'email'" not in definition
        _upgrade_to(_MODULE.revision)
        with engine.connect() as conn:
            assert table_exists(conn)
            assert column_exists(conn)
            definition, validated = provider_check(conn)
            assert "'email'" in definition and validated
            indexes = set(
                conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE tablename = 'email_signup_requests'"
                    )
                ).scalars()
            )
            assert {
                "ix_email_signup_requests_email",
                "ix_email_signup_requests_expires_at",
                "email_signup_requests_token_hash_key",
            } <= indexes

        with engine.begin() as conn:
            insert_email_row(conn, subject)
        with pytest.raises(RuntimeError, match="downgrade refused"):
            _downgrade_to(prior)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM signup_allowlist WHERE subject_id = :s"), {"s": subject})
            conn.execute(
                text(
                    "INSERT INTO users (email, user_id, role, timezone, locale, "
                    "is_initial_admin, provider_auto_link_blocked_at) "
                    "VALUES (:e, :u, 'user', 'UTC', 'en', false, now())"
                ),
                {"e": f"{blocked_user}@example.com", "u": blocked_user},
            )
        with pytest.raises(RuntimeError, match="provider_auto_link_blocked_at"):
            _downgrade_to(prior)
        with engine.connect() as conn:
            assert column_exists(conn)
            assert conn.execute(
                text("SELECT provider_auto_link_blocked_at FROM users WHERE user_id = :u"),
                {"u": blocked_user},
            ).scalar()
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET provider_auto_link_blocked_at = NULL WHERE user_id = :u"),
                {"u": blocked_user},
            )
        _downgrade_to(prior)
        with engine.connect() as conn:
            assert not table_exists(conn)
            assert not column_exists(conn)
            definition, validated = provider_check(conn)
            assert "'email'" not in definition and validated
        # Re-run: the upgrade's steps before the VALIDATE commit are re-runnable.
        _upgrade_to(_MODULE.revision)
        with engine.connect() as conn:
            assert table_exists(conn)
            assert column_exists(conn)
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM signup_allowlist WHERE subject_id = :s"), {"s": subject}
                    )
                    conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": blocked_user})
            finally:
                engine.dispose()
