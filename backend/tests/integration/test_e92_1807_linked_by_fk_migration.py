"""DB contract for the ``linked_by`` foreign-key migration e92 (#1807)."""

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

PRE_E92_REV = "e91_1008_public_ids"
E92_REV = "e92_1807_linked_by_fk"


def _user(conn: Connection) -> str:
    user_id = f"u-{uuid.uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO users (email, user_id, role, timezone, locale, is_initial_admin) "
            "VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{user_id}@test.example", "uid": user_id},
    )
    return user_id


def _link(conn: Connection, group_id: str, user_id: str, linked_by: str) -> None:
    conn.execute(
        text("INSERT INTO identity_links (group_id, user_id, linked_by) VALUES (:g, :u, :by)"),
        {"g": group_id, "u": user_id, "by": linked_by},
    )


def _linked_by(conn: Connection, user_id: str) -> str | None:
    return conn.execute(
        text("SELECT linked_by FROM identity_links WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()


class TestE92LinkedByForeignKey:
    def test_dangling_ids_are_nulled_and_deletes_set_null(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E92_REV)
            engine = _sync_engine()
            group = str(uuid.uuid4())
            with engine.begin() as conn:
                a, b, c, d = _user(conn), _user(conn), _user(conn), _user(conn)
                _link(conn, group, a, a)
                _link(conn, group, b, a)
                # What the delete_admin CLI left behind before #1807: an id
                # that names no account.
                _link(conn, group, c, "local:deleted-admin")
                # An account unlinked from the set before #1807: alive, in a
                # set of its own now, still named here.
                gone = _user(conn)
                _link(conn, group, d, gone)
                other_group = str(uuid.uuid4())
                _link(conn, other_group, gone, gone)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E92_REV)

            with engine.begin() as conn:
                assert _linked_by(conn, a) == a
                assert _linked_by(conn, b) == a
                assert _linked_by(conn, c) is None
                assert _linked_by(conn, d) is None
                assert _linked_by(conn, gone) == gone
                fk = conn.execute(
                    text(
                        "SELECT confdeltype FROM pg_constraint "
                        "WHERE conname = 'identity_links_linked_by_fkey'"
                    )
                ).scalar_one()
                assert fk == "n"  # ON DELETE SET NULL

            # Deleting the account that made the links keeps the others' rows.
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": a})
            with engine.begin() as conn:
                assert _linked_by(conn, b) is None
                assert _linked_by(conn, c) is None

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E92_REV)
            with engine.begin() as conn:
                # NULL falls back to the row's own account.
                assert _linked_by(conn, b) == b
                assert _linked_by(conn, c) == c
                nullable = conn.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'identity_links' AND column_name = 'linked_by'"
                    )
                ).scalar_one()
                assert nullable == "NO"
        finally:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")
