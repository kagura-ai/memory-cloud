"""Migration data-shape check for e14_655_allowlist_provider.

The general round-trip (upgrade-then-downgrade-then-upgrade) is already
covered by ``TestAlembicMigrations`` in the sibling
``test_alembic_migrations`` module. These tests add the data-specific
checks for #655:

- Existing rows backfill cleanly (``provider='github'``,
  ``subject_id=github_user_id``, ``subject_label=github_username``).
- The new ``(provider, subject_id, source)`` UNIQUE constraint fires on
  duplicates.
- Downgrade drops the new columns (data loss expected — DDL-only revert,
  matching the b03_396 convention this codebase has standardized on).
"""

from sqlalchemy import inspect, text

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)


class TestSignupAllowlistProviderMigration:
    """Data-shape checks for e14_655_allowlist_provider."""

    def test_upgrade_adds_columns_and_backfills_existing_rows(self):
        """Seed a pre-#655 row, upgrade, verify backfill + constraints."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            # Step 1: upgrade only to b04 (table creator). Tests in the
            # sibling module pin head-revision behavior; here we want a
            # known seedable state immediately BEFORE the e14 migration.
            command.upgrade(_get_alembic_config(), "e13_474_pricing_seeds")

            engine = _sync_engine()
            try:
                # Seed: one pre-#655 row mirroring what an admin add via
                # the legacy GitHub-only path produces.
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO signup_allowlist "
                            "(github_user_id, github_username, source, state) "
                            "VALUES ('583231', 'octocat', 'manual', 'active')"
                        )
                    )

                # Step 2: apply the new migration.
                command.upgrade(_get_alembic_config(), "e14_655_allowlist_provider")

                # Step 3: verify the seeded row now has all three new fields
                # populated from the backfill.
                with engine.begin() as conn:
                    row = conn.execute(
                        text(
                            "SELECT provider, subject_id, subject_label, "
                            "       github_user_id, github_username "
                            "FROM signup_allowlist "
                            "WHERE github_user_id = '583231'"
                        )
                    ).fetchone()
                assert row is not None
                provider, subject_id, subject_label, gh_uid, gh_uname = row
                assert provider == "github"
                assert subject_id == "583231"
                assert subject_label == "octocat"
                # Legacy columns left intact during the migration window.
                assert gh_uid == "583231"
                assert gh_uname == "octocat"
            finally:
                engine.dispose()

    def test_upgrade_replaces_unique_constraint(self):
        """The new (provider, subject_id, source) UNIQUE replaces the
        GitHub-only (github_user_id, source) one."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            inspector = inspect(engine)
            uniques = {uc["name"] for uc in inspector.get_unique_constraints("signup_allowlist")}
            assert "uq_allowlist_provider_subject_source" in uniques
            assert "uq_allowlist_user_source" not in uniques

            # The new index on (provider, subject_id) is present.
            index_names = {ix["name"] for ix in inspector.get_indexes("signup_allowlist")}
            assert "ix_signup_allowlist_provider_subject" in index_names
            assert "ix_signup_allowlist_github_user_id" not in index_names
        finally:
            engine.dispose()

    def test_upgrade_enforces_new_unique_constraint(self):
        """The same (provider, subject_id, source) triple cannot be inserted twice."""
        from sqlalchemy.exc import IntegrityError

        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                # First insert — should succeed.
                conn.execute(
                    text(
                        "INSERT INTO signup_allowlist "
                        "(provider, subject_id, subject_label, "
                        " github_user_id, github_username, source, state) "
                        "VALUES ('google', '108276939729829363', 'a@b.com', "
                        "        'google:108276939729829363', 'a@b.com', "
                        "        'manual', 'active')"
                    )
                )

            # Second insert with the same (provider, subject_id, source)
            # but a different legacy github_user_id MUST violate the new
            # composite UNIQUE — proving the constraint is on the new
            # columns, not just inherited from the old one.
            failed = False
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO signup_allowlist "
                            "(provider, subject_id, subject_label, "
                            " github_user_id, github_username, source, state) "
                            "VALUES ('google', '108276939729829363', 'a@b.com', "
                            "        'google:duplicate-attempt', 'a@b.com', "
                            "        'manual', 'active')"
                        )
                    )
            except IntegrityError:
                failed = True
            assert failed, "duplicate (provider, subject_id, source) should violate UNIQUE"
        finally:
            engine.dispose()

    def test_downgrade_drops_new_columns(self):
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
            command.downgrade(_get_alembic_config(), "e13_474_pricing_seeds")

        engine = _sync_engine()
        try:
            inspector = inspect(engine)
            columns = {col["name"] for col in inspector.get_columns("signup_allowlist")}
            assert "provider" not in columns
            assert "subject_id" not in columns
            assert "subject_label" not in columns
            # Old constraint + index restored on rollback.
            uniques = {uc["name"] for uc in inspector.get_unique_constraints("signup_allowlist")}
            assert "uq_allowlist_user_source" in uniques
            index_names = {ix["name"] for ix in inspector.get_indexes("signup_allowlist")}
            assert "ix_signup_allowlist_github_user_id" in index_names
        finally:
            engine.dispose()

        # Leave the DB at head so the rest of the integration suite sees a
        # clean, fully-migrated schema (same convention as the sibling
        # ``TestSignupGateMigration``).
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
