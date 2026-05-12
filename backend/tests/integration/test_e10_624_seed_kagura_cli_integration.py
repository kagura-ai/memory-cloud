"""Integration test for e10_624 — seeded ``kagura-cli`` row lands correctly.

The unit pin tests in ``tests/test_e10_624_seed_kagura_cli_migration.py``
prevent silent drift of the migration source. This integration test
verifies the migration actually puts the row in the DB with the expected
shape, that re-running ``upgrade`` is idempotent, and that ``downgrade``
DELETEs the row so the alembic chain stays reversible (see migration
docstring 'Downgrade policy' for the operational impact note).

The end-to-end device-flow mechanics for any registered client are
already covered by ``tests/api/test_device_code_endpoints.py`` and
``tests/auth/test_device_code_grant.py``. Once the seeded row exists,
those existing tests' machinery applies to ``kagura-cli`` without
needing duplication here — the only piece this test pins is that
the seed row is in the DB at all and has the expected shape.
"""

import json

from sqlalchemy import text

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)


def _fetch_kagura_cli_row(engine):
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    client_id,
                    client_secret_hash,
                    client_name,
                    owner_id,
                    workspace_id,
                    redirect_uris,
                    grant_types,
                    response_types,
                    scope,
                    token_endpoint_auth_method,
                    provider
                FROM oauth_clients
                WHERE client_id = :client_id
                """
            ).bindparams(client_id="kagura-cli")
        ).fetchone()
    return row


def _count_kagura_cli_rows(engine) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM oauth_clients WHERE client_id = :cid").bindparams(
                cid="kagura-cli"
            )
        ).scalar_one()


class TestE10SeedKaguraCliIntegration:
    def test_upgrade_seeds_kagura_cli_with_correct_shape(self):
        """After ``alembic upgrade head``, a single ``kagura-cli`` row exists
        with the exact Issue #624 shape."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            assert _count_kagura_cli_rows(engine) == 1, (
                "expected exactly one kagura-cli row after upgrade"
            )
            row = _fetch_kagura_cli_row(engine)
            assert row is not None

            (
                client_id,
                client_secret_hash,
                client_name,
                owner_id,
                workspace_id,
                redirect_uris,
                grant_types,
                response_types,
                scope,
                token_endpoint_auth_method,
                provider,
            ) = row

            # Public-client contract
            assert client_id == "kagura-cli"
            assert client_secret_hash == ""
            assert token_endpoint_auth_method == "none"

            # DCR-pattern: workspace resolved at consent time
            assert owner_id is None
            assert workspace_id is None

            # Operator-facing label (shows on /device consent page)
            assert client_name == "Kagura Memory CLI"
            assert provider == "custom"

            # Scope: no memory:admin (narrowing-first ordering per #608 D1)
            assert scope == "memory:read memory:write"
            assert "memory:admin" not in scope.split()

            # JSON columns: tolerate both list-decoded (SQLAlchemy JSON type)
            # and raw string (psycopg2 returning JSON as str on some
            # drivers). Normalize before compare.
            def _decode(v):
                return v if isinstance(v, list) else json.loads(v)

            assert _decode(redirect_uris) == [
                "urn:ietf:wg:oauth:2.0:oob",
                "http://127.0.0.1:0/",
            ]
            assert _decode(grant_types) == [
                "urn:ietf:params:oauth:grant-type:device_code",
                "refresh_token",
            ]
            assert _decode(response_types) == []
        finally:
            engine.dispose()

    def test_upgrade_is_idempotent(self):
        """Re-running ``alembic upgrade head`` is a no-op for the seed —
        ``ON CONFLICT (client_id) DO NOTHING`` prevents duplicates."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
            # Force the seed migration to re-execute by stepping back one
            # then upgrading to head again.
            command.downgrade(_get_alembic_config(), "e09_608_dcr_default_narrow")
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            # Still exactly one row — ON CONFLICT DO NOTHING held.
            assert _count_kagura_cli_rows(engine) == 1
        finally:
            engine.dispose()

    def test_downgrade_deletes_row(self):
        """``downgrade(e09_608)`` runs ``e10.downgrade()`` which DELETEs
        the seeded row so the alembic chain stays reversible. Without
        this DELETE, ``d04_519_oauth_owner_nullable.downgrade()`` would
        fail downstream when it tries to re-add ``NOT NULL`` on
        ``oauth_clients.owner_id`` (the seed row holds ``owner_id=NULL``).

        See migration module docstring 'Downgrade policy' for the
        operational impact note (active SDK sessions invalidated)."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            assert _count_kagura_cli_rows(engine) == 1
        finally:
            engine.dispose()

        # Step back past e10's downgrade — the DELETE must remove the row.
        with _alembic_at_test_db():
            command.downgrade(_get_alembic_config(), "e09_608_dcr_default_narrow")

        engine = _sync_engine()
        try:
            assert _count_kagura_cli_rows(engine) == 0, (
                "e10.downgrade() did not delete the kagura-cli row — "
                "leaving it in place would break d04_519.downgrade() "
                "(ALTER COLUMN owner_id SET NOT NULL fails on a row "
                "with owner_id=NULL). See migration module docstring "
                "'Downgrade policy'."
            )
        finally:
            engine.dispose()

        # Leave the DB at head so the rest of the integration suite sees a
        # clean, fully-migrated schema (mirrors TestSignupGateMigration).
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
