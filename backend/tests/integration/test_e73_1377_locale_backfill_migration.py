"""DB contract for the locale backfill migration e73 (#1377)."""

import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PRE_E73_REV = "e72_1365_secret_log_carveout"
E73_REV = "e73_1377_locale_backfill"


def _seed_connector(conn: Connection, locale: str | None) -> str:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"u-{suffix}"
    workspace_id = str(uuid.uuid4())
    resource_pk = str(uuid.uuid4())
    connector_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO users "
            "(email, user_id, role, timezone, locale, is_initial_admin) "
            "VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{user_id}@test.example", "uid": user_id},
    )
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": workspace_id, "name": f"ws-{suffix}", "owner": user_id},
    )
    conn.execute(
        text("INSERT INTO resources (id, workspace_id, resource_id) VALUES (:id, :ws, :slug)"),
        {"id": resource_pk, "ws": workspace_id, "slug": f"r-{suffix}"},
    )
    conn.execute(
        text(
            "INSERT INTO workspace_connectors "
            "(id, resource_pk, workspace_id, connector_type, locale) "
            "VALUES (:id, :resource, :workspace, 'slack', :locale)"
        ),
        {
            "id": connector_id,
            "resource": resource_pk,
            "workspace": workspace_id,
            "locale": locale,
        },
    )
    return connector_id


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE73LocaleBackfillMigration:
    def test_backfill_normalizes_variants_and_bumps_only_rewritten_rows(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E73_REV)
            engine = _sync_engine()
            # (stored locale, expected post-migration locale, expect version bump)
            cases = [
                ("ja-JP", "ja", True),
                ("EN_us", "en", True),
                # U+3000 full-width space — the btrim-vs-strip divergence the
                # Python row-wise implementation exists for (review finding):
                # an SQL btrim would have downgraded this row to NULL.
                ("　ja", "ja", True),
                ("fr", None, True),
                ("ja", "ja", False),  # already conforming — untouched, no bump
                (None, None, False),
            ]
            with engine.begin() as conn:
                seeded = [
                    (_seed_connector(conn, stored), stored, expected, bumped)
                    for stored, expected, bumped in cases
                ]

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E73_REV)

            with engine.begin() as conn:
                for connector_id, stored, expected, bumped in seeded:
                    row = conn.execute(
                        text(
                            "SELECT locale, config_version FROM workspace_connectors WHERE id = :id"
                        ),
                        {"id": connector_id},
                    ).one()
                    assert row.locale == expected, (
                        f"stored {stored!r}: expected {expected!r}, got {row.locale!r}"
                    )
                    # Seed default config_version is 1; a rewritten row must
                    # bump to 2 so a bridge holding the old ETag refetches.
                    assert row.config_version == (2 if bumped else 1), (
                        f"stored {stored!r}: config_version {row.config_version}"
                    )
        finally:
            _leave_db_at_head()
