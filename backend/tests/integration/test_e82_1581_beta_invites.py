"""DB contract for e82 (#1581): ``beta_invites`` + widened allowlist ``source``.

- upgrade creates ``beta_invites`` (unique ``token_hash``, inviter index) and
  lets ``signup_allowlist.source`` hold ``'beta_invite'``;
- deleting the inviter cascades their invites; deleting the redeemed allowlist
  row only NULLs the back-reference (the invite stays redeemed);
- downgrade refuses while a ``source='beta_invite'`` allowlist row exists (the
  narrower CHECK could not be restored), then drops the table and re-narrows.
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

PRE_E82_REV = "e81_1569_analysis_lane_cols"
E82_REV = "e82_1581_beta_invites"


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


def _seed_allowlist(conn: Connection, *, source: str, added_by: str) -> str:
    sub = uuid.uuid4().hex
    return str(
        conn.execute(
            text(
                "INSERT INTO signup_allowlist (provider, subject_id, subject_label,"
                " github_user_id, github_username, source, state, added_by_user_id)"
                " VALUES ('google', :sub, 'invitee@test.example', :legacy,"
                " 'invitee@test.example', :source, 'active', :added_by) RETURNING id"
            ),
            {"sub": sub, "legacy": f"google:{sub}"[:64], "source": source, "added_by": added_by},
        ).scalar_one()
    )


def _seed_invite(conn: Connection, *, inviter: str, token_hash: str | None = None) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO beta_invites (token_hash, inviter_user_id, expires_at)"
                " VALUES (:h, :uid, now() + interval '7 days') RETURNING id"
            ),
            {"h": token_hash or uuid.uuid4().hex * 2, "uid": inviter},
        ).scalar_one()
    )


def _table_exists(conn: Connection, name: str) -> bool:
    return (
        conn.execute(text("SELECT to_regclass(:name)"), {"name": f"public.{name}"}).scalar_one()
        is not None
    )


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE82BetaInvites:
    def test_upgrade_creates_table_and_downgrade_guards_beta_invite_rows(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E82_REV)
            engine = _sync_engine()

            with engine.begin() as conn:
                assert not _table_exists(conn, "beta_invites")
                inviter = _seed_user(conn)

            # Before e82 the CHECK rejects the new source value.
            with pytest.raises(IntegrityError, match="valid_signup_allowlist_source"):
                with engine.begin() as conn:
                    _seed_allowlist(conn, source="beta_invite", added_by=inviter)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E82_REV)

            with engine.begin() as conn:
                assert _table_exists(conn, "beta_invites")
                entry_id = _seed_allowlist(conn, source="beta_invite", added_by=inviter)
                # The pre-existing sources are still admitted.
                _seed_allowlist(conn, source="manual", added_by=inviter)
                invite_id = _seed_invite(conn, inviter=inviter, token_hash="a" * 64)
                conn.execute(
                    text(
                        "UPDATE beta_invites SET redeemed_at = now(),"
                        " redeemed_allowlist_entry_id = :entry WHERE id = :id"
                    ),
                    {"entry": entry_id, "id": invite_id},
                )

            # token_hash is the lookup key — it must be unique.
            with pytest.raises(IntegrityError, match="uq_beta_invites_token_hash"):
                with engine.begin() as conn:
                    _seed_invite(conn, inviter=inviter, token_hash="a" * 64)

            # Downgrade refuses while a beta_invite allowlist row exists.
            with pytest.raises(RuntimeError, match="source='beta_invite'"):
                with _alembic_at_test_db():
                    command.downgrade(_get_alembic_config(), PRE_E82_REV)

            with engine.begin() as conn:
                # Pruning the allowlist row keeps the invite, minus the pointer.
                conn.execute(text("DELETE FROM signup_allowlist WHERE id = :id"), {"id": entry_id})
                row = conn.execute(
                    text(
                        "SELECT redeemed_at, redeemed_allowlist_entry_id"
                        " FROM beta_invites WHERE id = :id"
                    ),
                    {"id": invite_id},
                ).one()
                assert row.redeemed_at is not None
                assert row.redeemed_allowlist_entry_id is None

                # Erasing the inviter takes their invites with them.
                conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": inviter})
                remaining = conn.execute(
                    text("SELECT count(*) FROM beta_invites WHERE id = :id"), {"id": invite_id}
                ).scalar_one()
                assert remaining == 0

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E82_REV)

            with engine.begin() as conn:
                assert not _table_exists(conn, "beta_invites")
                inviter = _seed_user(conn)
            with pytest.raises(IntegrityError, match="valid_signup_allowlist_source"):
                with engine.begin() as conn:
                    _seed_allowlist(conn, source="beta_invite", added_by=inviter)
        finally:
            _leave_db_at_head()
