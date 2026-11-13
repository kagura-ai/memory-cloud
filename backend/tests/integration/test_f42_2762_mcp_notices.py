"""DB contract for f42 (#2762): ``mcp_notices``, ``mcp_notice_deliveries``, ``mcp_notice_consents``.

- upgrade creates the three tables with their CHECKs (text size in characters
  and bytes, the window, the consent source, a "yes" names its wording), the
  partial active index, the per-user consent index and the deliveries'
  ``(user_id, notice_id)`` primary key;
- consent and delivery rows cascade with their user; deliveries cascade with
  their notice;
- downgrade drops the three tables and leaves ``users`` untouched;
- the round trip is repeatable.

The revision before f42 is read from the migration module itself. Not
executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DataError, IntegrityError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "f42_2762_mcp_notices.py"
)
TABLES = ("mcp_notices", "mcp_notice_deliveries", "mcp_notice_consents")


def _migration_module():
    spec = importlib.util.spec_from_file_location("_f42_2762_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _migration_module()
F42_REV = _MODULE.revision
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


def _notice(conn: Connection, body: str = "Maintenance on Saturday.", days: int = 3) -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO mcp_notices (text, starts_at, expires_at)"
            " VALUES (:text, now(), now() + make_interval(days => :days)) RETURNING id"
        ),
        {"text": body, "days": days},
    ).scalar_one()


def _consent(
    conn: Connection,
    *,
    user_id: str,
    value: bool = True,
    version: str | None = "2026-11",
    source: str = "settings",
) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mcp_notice_consents (user_id, value, wording_version, source)"
                " VALUES (:uid, :value, :version, :source) RETURNING id"
            ),
            {"uid": user_id, "value": value, "version": version, "source": source},
        ).scalar_one()
    )


def _deliver(conn: Connection, user_id: str, notice_id: uuid.UUID) -> None:
    conn.execute(
        text("INSERT INTO mcp_notice_deliveries (user_id, notice_id) VALUES (:uid, :nid)"),
        {"uid": user_id, "nid": notice_id},
    )


def _exists(conn: Connection, table: str) -> bool:
    return conn.execute(text(f"SELECT to_regclass('public.{table}')")).scalar_one() is not None


def _count(conn: Connection, table: str, user_id: str) -> int:
    return conn.execute(
        text(f"SELECT count(*) FROM {table} WHERE user_id = :uid"), {"uid": user_id}
    ).scalar_one()


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestF42McpNotices:
    def test_revision_ids(self) -> None:
        assert F42_REV == "f42_2762_mcp_notices"
        assert len(F42_REV) <= 32
        assert PRIOR_REV == "f41_2768_llm_call_log_lane_idx"

    def test_upgrade_creates_the_tables_and_downgrade_drops_them(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRIOR_REV)
            engine = _sync_engine()

            with engine.begin() as conn:
                assert not any(_exists(conn, t) for t in TABLES)
                user_id = _seed_user(conn)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), F42_REV)

            with engine.begin() as conn:
                assert all(_exists(conn, t) for t in TABLES)
                columns = dict(
                    conn.execute(
                        text(
                            "SELECT column_name, data_type FROM information_schema.columns"
                            " WHERE table_name = 'mcp_notices'"
                        )
                    ).all()
                )
                assert columns["id"] == "uuid"
                assert columns["starts_at"] == "timestamp with time zone"
                assert columns["withdrawn_at"] == "timestamp with time zone"
                active = conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_mcp_notices_active'"
                    )
                ).scalar_one()
                assert "(expires_at)" in active and "withdrawn_at IS NULL" in active
                consent_index = conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes"
                        " WHERE indexname = 'ix_mcp_notice_consents_user_id_id'"
                    )
                ).scalar_one()
                assert "(user_id, id)" in consent_index
                pk = conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes"
                        " WHERE tablename = 'mcp_notice_deliveries' AND indexname LIKE '%pkey'"
                    )
                ).scalar_one()
                assert "(user_id, notice_id)" in pk

                notice_id = _notice(conn)
                japanese = _notice(conn, body="お" * 300)
                assert japanese
                first = _consent(conn, user_id=user_id)
                second = _consent(conn, user_id=user_id, value=False, version=None)
                assert second > first
                _deliver(conn, user_id, notice_id)

            # Once per (user, notice).
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    _deliver(conn, user_id, notice_id)

            for body in ("", "\U0001f600" * 226):
                with pytest.raises(IntegrityError, match="ck_mcp_notices_text_size"):
                    with engine.begin() as conn:
                        _notice(conn, body=body)
            # More than 300 characters does not fit the column at all.
            with pytest.raises(DataError):
                with engine.begin() as conn:
                    _notice(conn, body="a" * 301)

            with pytest.raises(IntegrityError, match="ck_mcp_notices_window"):
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO mcp_notices (text, starts_at, expires_at)"
                            " VALUES ('x', now(), now())"
                        )
                    )

            with pytest.raises(IntegrityError, match="valid_mcp_notice_consent_source"):
                with engine.begin() as conn:
                    _consent(conn, user_id=user_id, source="signup")

            with pytest.raises(IntegrityError, match="ck_mcp_notice_consent_version_when_on"):
                with engine.begin() as conn:
                    _consent(conn, user_id=user_id, value=True, version=None)

            # A delivery goes with its notice.
            with engine.begin() as conn:
                other = _seed_user(conn)
                _deliver(conn, other, japanese)
                conn.execute(text("DELETE FROM mcp_notices WHERE id = :id"), {"id": japanese})
                assert _count(conn, "mcp_notice_deliveries", other) == 0

            # Consents and deliveries go with the user; the notice stays.
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
                assert _count(conn, "mcp_notice_consents", user_id) == 0
                assert _count(conn, "mcp_notice_deliveries", user_id) == 0
                assert (
                    conn.execute(
                        text("SELECT count(*) FROM mcp_notices WHERE id = :id"), {"id": notice_id}
                    ).scalar_one()
                    == 1
                )

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRIOR_REV)

            with engine.begin() as conn:
                assert not any(_exists(conn, t) for t in TABLES)
                assert (
                    conn.execute(
                        text("SELECT count(*) FROM users WHERE user_id = :uid"), {"uid": other}
                    ).scalar_one()
                    == 1
                )

            # Repeatable.
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), F42_REV)
            with engine.begin() as conn:
                assert all(_exists(conn, t) for t in TABLES)
                conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": other})
        finally:
            _leave_db_at_head()
