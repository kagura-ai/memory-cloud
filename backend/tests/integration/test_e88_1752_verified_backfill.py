"""DB contract for e88 (#1752): ``users.email_verified_at`` is back-filled
again for OAuth accounts created after the e86 back-fill.

- an account with a linked provider and no ``email_verified_at`` is marked;
- an already verified account keeps its timestamp;
- ``@local`` addresses and accounts without a provider are never marked;
- downgrade changes nothing.
"""

from sqlalchemy import text

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)
from tests.integration.test_e86_1678_email_password import (
    _leave_db_at_head,
    _seed_user,
    _verified_at,
)

PRE_E88_REV = "e87_1738_purge_indexes"
E88_REV = "e88_1752_verified_backfill"


class TestE88VerifiedBackfill:
    def test_backfill_and_downgrade(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E88_REV)
            engine = _sync_engine()

            with engine.begin() as conn:
                # Created by an OAuth sign-in after e86 ran: provider, no timestamp.
                oauth_since_e86 = _seed_user(conn, provider="google")
                already_verified = _seed_user(conn, provider="github")
                conn.execute(
                    text(
                        "UPDATE users SET email_verified_at = '2026-01-01 00:00:00'"
                        " WHERE user_id = :uid"
                    ),
                    {"uid": already_verified},
                )
                password_only = _seed_user(conn)
                cli_admin = _seed_user(conn, email="Admin@LOCAL", provider="google")

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E88_REV)

            with engine.begin() as conn:
                assert _verified_at(conn, oauth_since_e86) is not None
                assert str(_verified_at(conn, already_verified)) == "2026-01-01 00:00:00"
                assert _verified_at(conn, password_only) is None
                assert _verified_at(conn, cli_admin) is None

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E88_REV)

            with engine.begin() as conn:
                assert _verified_at(conn, oauth_since_e86) is not None
        finally:
            _leave_db_at_head()
