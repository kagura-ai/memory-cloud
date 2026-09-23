"""DB contract for e85 (#1665): ``terms_acceptances``.

- upgrade creates the table with a tz-aware ``accepted_at`` defaulting to now,
  the ``source`` CHECK and the ``(user_id, accepted_at)`` index;
- rows cascade with their user;
- downgrade drops the table and leaves ``users`` untouched;
- the round trip is repeatable.
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

PRE_E85_REV = "e84_1619_tool_guardrails"
E85_REV = "e85_1665_terms_acceptances"


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


def _insert(conn: Connection, *, user_id: str, source: str = "login") -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO terms_acceptances (user_id, version, source)"
                " VALUES (:uid, '2026-09', :source) RETURNING id"
            ),
            {"uid": user_id, "source": source},
        ).scalar_one()
    )


def _table_exists(conn: Connection) -> bool:
    return (
        conn.execute(text("SELECT to_regclass('public.terms_acceptances')")).scalar_one()
        is not None
    )


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE85TermsAcceptances:
    def test_upgrade_creates_the_table_and_downgrade_drops_it(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E85_REV)
            engine = _sync_engine()

            with engine.begin() as conn:
                assert not _table_exists(conn)
                user_id = _seed_user(conn)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E85_REV)

            with engine.begin() as conn:
                assert _table_exists(conn)
                accepted_at_type = conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns"
                        " WHERE table_name = 'terms_acceptances'"
                        " AND column_name = 'accepted_at'"
                    )
                ).scalar_one()
                assert accepted_at_type == "timestamp with time zone"
                index = conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes"
                        " WHERE indexname = 'ix_terms_acceptances_user_accepted_at'"
                    )
                ).scalar_one()
                assert "(user_id, accepted_at)" in index

                row_id = _insert(conn, user_id=user_id)
                accepted_at = conn.execute(
                    text("SELECT accepted_at FROM terms_acceptances WHERE id = :id"),
                    {"id": row_id},
                ).scalar_one()
                assert accepted_at is not None and accepted_at.tzinfo is not None

            # The source vocabulary is closed.
            with pytest.raises(IntegrityError, match="valid_terms_acceptance_source"):
                with engine.begin() as conn:
                    _insert(conn, user_id=user_id, source="api")

            # The history goes with the user.
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
                remaining = conn.execute(
                    text("SELECT count(*) FROM terms_acceptances WHERE user_id = :uid"),
                    {"uid": user_id},
                ).scalar_one()
                assert remaining == 0
                survivor = _seed_user(conn)
                _insert(conn, user_id=survivor, source="reaccept")

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E85_REV)

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
                command.upgrade(_get_alembic_config(), E85_REV)
            with engine.begin() as conn:
                assert _table_exists(conn)
                conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": survivor})
        finally:
            _leave_db_at_head()
