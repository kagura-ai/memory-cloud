"""DB contract for e83 (#1595): ``beta_invites.label``.

- upgrade adds a NULLable ``VARCHAR(100)`` — existing rows keep working with no
  backfill, and the column enforces the same bound the API validates;
- downgrade drops the column and leaves the rows (and their lifecycle) intact;
- the round trip is repeatable.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DataError, ProgrammingError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PRE_E83_REV = "e82_1581_beta_invites"
E83_REV = "e83_1595_beta_invite_label"


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


def _seed_invite(conn: Connection, *, inviter: str, label: str | None = None) -> str:
    columns = "token_hash, inviter_user_id, expires_at" + (", label" if label is not None else "")
    values = ":h, :uid, now() + interval '7 days'" + (", :label" if label is not None else "")
    params = {"h": uuid.uuid4().hex * 2, "uid": inviter}
    if label is not None:
        params["label"] = label
    return str(
        conn.execute(
            text(f"INSERT INTO beta_invites ({columns}) VALUES ({values}) RETURNING id"), params
        ).scalar_one()
    )


def _label_column(conn: Connection):
    return conn.execute(
        text(
            "SELECT data_type, character_maximum_length, is_nullable, column_default"
            " FROM information_schema.columns"
            " WHERE table_name = 'beta_invites' AND column_name = 'label'"
        )
    ).one_or_none()


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE83BetaInviteLabel:
    def test_upgrade_adds_a_nullable_varchar_100_and_downgrade_drops_it(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E83_REV)
            engine = _sync_engine()

            with engine.begin() as conn:
                assert _label_column(conn) is None
                inviter = _seed_user(conn)
                legacy_id = _seed_invite(conn, inviter=inviter)

            # Before e83 the column does not exist.
            with pytest.raises(ProgrammingError, match="label"):
                with engine.begin() as conn:
                    _seed_invite(conn, inviter=inviter, label="Alice")

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E83_REV)

            with engine.begin() as conn:
                column = _label_column(conn)
                assert column is not None
                assert column.data_type == "character varying"
                assert column.character_maximum_length == 100
                assert column.is_nullable == "YES"
                assert column.column_default is None
                # No backfill: a pre-existing row simply has no label.
                legacy_label = conn.execute(
                    text("SELECT label FROM beta_invites WHERE id = :id"), {"id": legacy_id}
                ).scalar_one()
                assert legacy_label is None
                labelled_id = _seed_invite(conn, inviter=inviter, label="あ" * 100)

            # The column holds the API's bound — 100 characters, not bytes.
            with pytest.raises(DataError):
                with engine.begin() as conn:
                    _seed_invite(conn, inviter=inviter, label="x" * 101)

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E83_REV)

            with engine.begin() as conn:
                assert _label_column(conn) is None
                # The rows survive the downgrade; only the labels are gone.
                remaining = conn.execute(
                    text("SELECT count(*) FROM beta_invites WHERE id IN (:a, :b)"),
                    {"a": legacy_id, "b": labelled_id},
                ).scalar_one()
                assert remaining == 2

            # Repeatable.
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E83_REV)
            with engine.begin() as conn:
                assert _label_column(conn) is not None
                conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": inviter})
        finally:
            _leave_db_at_head()
