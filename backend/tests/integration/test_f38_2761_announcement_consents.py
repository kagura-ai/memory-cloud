"""DB contract for f38 (#2761): ``announcement_email_consents``.

- upgrade creates the table with a BIGSERIAL id, a tz-aware ``recorded_at``
  defaulting to now, the ``source`` CHECK, the "a yes names its wording" CHECK
  and the ``(user_id, id)`` index;
- rows cascade with their user;
- downgrade drops the table and leaves ``users`` untouched;
- the round trip is repeatable.

The revision before f38 is read from the migration module itself, so
re-chaining f38 onto another parent changes one line in the migration and
nothing here.

A file of its own, like the other ``test_<revision>_...`` files:
``_reset_alembic_state`` rebuilds the schema through every migration. Not
executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

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

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "f38_2761_announcement_consents.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("_f38_2761_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _migration_module()
F38_REV = _MODULE.revision
PRIOR_REV = _MODULE.down_revision


def _seed_user(conn: Connection) -> str:
    user_id = f"u-{uuid.uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO users (email, user_id, role, timezone, locale, is_initial_admin)"
            " VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{user_id}@test.example", "uid": user_id},
    )
    return user_id


def _insert(
    conn: Connection,
    *,
    user_id: str,
    value: bool = True,
    version: str | None = "2026-11",
    source: str = "signup",
) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO announcement_email_consents (user_id, value, wording_version, source)"
                " VALUES (:uid, :value, :version, :source) RETURNING id"
            ),
            {"uid": user_id, "value": value, "version": version, "source": source},
        ).scalar_one()
    )


def _table_exists(conn: Connection) -> bool:
    return (
        conn.execute(text("SELECT to_regclass('public.announcement_email_consents')")).scalar_one()
        is not None
    )


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestF38AnnouncementConsents:
    def test_revision_ids(self) -> None:
        assert F38_REV == "f38_2761_announcement_consents"
        assert len(F38_REV) <= 32
        assert PRIOR_REV

    def test_upgrade_creates_the_table_and_downgrade_drops_it(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRIOR_REV)
            engine = _sync_engine()

            with engine.begin() as conn:
                assert not _table_exists(conn)
                user_id = _seed_user(conn)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), F38_REV)

            with engine.begin() as conn:
                assert _table_exists(conn)
                columns = dict(
                    conn.execute(
                        text(
                            "SELECT column_name, data_type FROM information_schema.columns"
                            " WHERE table_name = 'announcement_email_consents'"
                        )
                    ).all()
                )
                assert columns["id"] == "bigint"
                assert columns["value"] == "boolean"
                assert columns["recorded_at"] == "timestamp with time zone"
                index = conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes"
                        " WHERE indexname = 'ix_announcement_email_consents_user_id_id'"
                    )
                ).scalar_one()
                assert "(user_id, id)" in index

                first = _insert(conn, user_id=user_id)
                second = _insert(
                    conn, user_id=user_id, value=False, version=None, source="settings"
                )
                assert second > first
                recorded_at = conn.execute(
                    text("SELECT recorded_at FROM announcement_email_consents WHERE id = :id"),
                    {"id": first},
                ).scalar_one()
                assert recorded_at is not None and recorded_at.tzinfo is not None

            # The source vocabulary is closed.
            with pytest.raises(IntegrityError, match="valid_announcement_consent_source"):
                with engine.begin() as conn:
                    _insert(conn, user_id=user_id, source="api")

            # A "yes" must name the wording the person saw.
            with pytest.raises(IntegrityError, match="ck_announcement_consent_version_when_on"):
                with engine.begin() as conn:
                    _insert(conn, user_id=user_id, value=True, version=None)

            # The history goes with the user.
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
                remaining = conn.execute(
                    text("SELECT count(*) FROM announcement_email_consents WHERE user_id = :uid"),
                    {"uid": user_id},
                ).scalar_one()
                assert remaining == 0
                survivor = _seed_user(conn)
                _insert(
                    conn, user_id=survivor, source="unsubscribe_link", value=False, version=None
                )

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRIOR_REV)

            with engine.begin() as conn:
                assert not _table_exists(conn)
                # The users row is untouched by the downgrade.
                assert (
                    conn.execute(
                        text("SELECT count(*) FROM users WHERE user_id = :uid"),
                        {"uid": survivor},
                    ).scalar_one()
                    == 1
                )

            # Repeatable.
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), F38_REV)
            with engine.begin() as conn:
                assert _table_exists(conn)
                conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": survivor})
        finally:
            _leave_db_at_head()
