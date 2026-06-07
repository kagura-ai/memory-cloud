"""Integration tests for migration ``e37_517_user_oauth_providers`` (#517).

Covers the data-shape and provider backfill that ``TestAlembicMigrations``
does not — specifically:

1. ``user_oauth_providers`` table is created (asserted via
   ``information_schema.tables`` — the backend test image has NO
   postgresql-client, so no pg_dump/psql).
2. Backfill contract (edge case 4 from gate1): a user with
   ``auth_provider='google'`` gets exactly one ``user_oauth_providers`` row
   with ``oauth_sub == user_id``.
3. A user with ``auth_provider IS NULL`` (pre-#361 legacy) gets NO row.
4. A user with ``auth_method='password'`` gets NO row even when
   ``auth_provider`` is set — they self-heal via ensure_user dual-read on
   next login.
5. ``downgrade()`` drops the table.

The backfill is re-triggered deterministically per test by resetting the
alembic state, upgrading to the pre-revision (table absent), seeding ``users``
rows, then upgrading to head — the same mechanism used by
``test_e15_675_workspace_slot_bonus_migration``. This does NOT rely on the
session-scoped ``db_session`` fixture, so backfill runs against freshly
seeded rows every time.

Pre-revision is ``e36_888_retrieval_feedback``.
"""

import uuid

from sqlalchemy import inspect, text

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

# Pre-e37 revision: state where the ``user_oauth_providers`` table does NOT
# yet exist. Pinning this target keeps the test correct as more migrations
# land on top of e37.
PRE_E37_REV = "e36_888_retrieval_feedback"

_TABLE = "user_oauth_providers"


def _seed_user(
    conn,
    *,
    auth_provider: str | None,
    auth_method: str = "oauth",
    user_id: str | None = None,
) -> str:
    """Insert a minimal user row; return the user_id (OAuth sub).

    ``users.timezone``, ``users.locale``, and ``users.is_initial_admin`` are
    NOT NULL columns without server defaults in the baseline schema, so the
    raw-SQL INSERT (below the ORM Python-side defaults) must supply them
    explicitly. ``created_at`` has a server_default of now(), so the
    backfill's ``COALESCE(created_at, now())`` always resolves to created_at.
    """
    uid = user_id or f"u-{uuid.uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO users "
            "(email, user_id, role, timezone, locale, is_initial_admin, "
            " auth_method, auth_provider) "
            "VALUES (:email, :uid, 'user', 'UTC', 'en', false, "
            " :auth_method, :auth_provider)"
        ),
        {
            "email": f"{uid}@test.example",
            "uid": uid,
            "auth_method": auth_method,
            "auth_provider": auth_provider,
        },
    )
    return uid


def _provider_rows(conn, user_id: str) -> list:
    return conn.execute(
        text("SELECT provider, oauth_sub FROM user_oauth_providers WHERE user_id = :uid"),
        {"uid": user_id},
    ).fetchall()


def _leave_db_at_head() -> None:
    """Convention: integration suite expects the test DB at head after each test."""
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE37UserOAuthProvidersMigration:
    """Data-shape and provider-backfill checks for e37_517."""

    def test_upgrade_creates_table(self):
        """``user_oauth_providers`` exists after upgrade (information_schema)."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                exists = conn.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = :t)"
                    ),
                    {"t": _TABLE},
                ).scalar_one()
            assert exists is True

            # Index on user_id is present under its declared name.
            inspector = inspect(engine)
            index_names = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
            assert "ix_user_oauth_providers_user_id" in index_names
        finally:
            engine.dispose()

    def test_backfill_google_user_gets_row(self):
        """A user with auth_provider='google' gets a row (oauth_sub == user_id)."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E37_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn, auth_provider="google")

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            with engine.begin() as conn:
                rows = _provider_rows(conn, uid)
            assert len(rows) == 1
            assert rows[0].provider == "google"
            assert rows[0].oauth_sub == uid
        finally:
            engine.dispose()

    def test_backfill_github_user_gets_row(self):
        """A user with auth_provider='github' gets a row (oauth_sub == user_id)."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E37_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn, auth_provider="github")

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            with engine.begin() as conn:
                rows = _provider_rows(conn, uid)
            assert len(rows) == 1
            assert rows[0].provider == "github"
            assert rows[0].oauth_sub == uid
        finally:
            engine.dispose()

    def test_backfill_null_provider_user_gets_no_row(self):
        """A user with auth_provider IS NULL (pre-#361 legacy) gets NO row."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E37_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn, auth_provider=None)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            with engine.begin() as conn:
                rows = _provider_rows(conn, uid)
            assert rows == []
        finally:
            engine.dispose()

    def test_backfill_password_user_gets_no_row(self):
        """A password user gets NO row even when auth_provider is set.

        Isolates the ``auth_method <> 'password'`` guard (edge case 4): the
        provider column alone is not sufficient to backfill — password-auth
        users resolve via ensure_user dual-read and self-heal on next login.
        """
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E37_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn, auth_provider="google", auth_method="password")

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            with engine.begin() as conn:
                rows = _provider_rows(conn, uid)
            assert rows == []
        finally:
            engine.dispose()

    def test_downgrade_drops_table(self):
        """Downgrade removes the user_oauth_providers table."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
            command.downgrade(_get_alembic_config(), PRE_E37_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                exists = conn.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = :t)"
                    ),
                    {"t": _TABLE},
                ).scalar_one()
            assert exists is False
        finally:
            engine.dispose()
            _leave_db_at_head()
