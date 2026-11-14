"""DB contract for the admission-record migration f45 (#2803).

Upgrades a clean database to f43, seeds raw rows for every kind of evidence
and every conflict, then upgrades to f45 and checks what each account reads:

- ``admin_cli``: a CLI-created local admin (even with ``is_initial_admin``);
- the ``user.signed_up`` audit row of an e-mail account, for each admission;
- ``first_user``: ``is_initial_admin`` on the lowest row only;
- ``beta_invite``: the creating identity's ``beta_invite`` allowlist row (user-
  and admin-minted), the ``beta_invite.redeemed`` audit row after the
  allowlist row was pruned, and an invite that is gone (details NULL);
- ``allowlist``: a manual row from before the account (one from after it
  does not count), never a linked identity's row;
- ``workspace_invitation``: a ``signup_admitted`` audit row;
- ``unknown``: everything else;
- conflicts: manual-before-account beats ``signup_admitted``; a beta invite
  beats a manual row.

The log carries counts only (no id, address, invite id or label), no row's
``updated_at`` moves, the downgrade removes the columns and constraints, and
a re-upgrade rebuilds the record from the evidence.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PRE_REV = "f43_2798_paid_integrations"
REV = "f45_2803_admission_record"
LABEL = "SENTINEL-LABEL-f45-do-not-log"

_CHECKS = (
    "valid_admitted_via",
    "valid_admitted_invite_issuer",
    "admitted_invite_fields_beta_only",
    "admitted_by_user_issuer_user",
)


def _user(
    conn: Connection,
    user_id: str,
    *,
    role: str = "user",
    login_id: str | None = None,
    is_initial_admin: bool = False,
    auth_method: str = "oauth",
) -> str:
    conn.execute(
        text(
            "INSERT INTO users (email, user_id, role, timezone, locale, is_initial_admin, "
            "login_id, auth_method) "
            "VALUES (:email, :uid, :role, 'UTC', 'en', :initial, :login, :method)"
        ),
        {
            "email": f"{user_id.replace(':', '-')}@f45.example",
            "uid": user_id,
            "role": role,
            "initial": is_initial_admin,
            "login": login_id,
            "method": auth_method,
        },
    )
    return user_id


def _provider(conn: Connection, user_id: str, provider: str, sub: str) -> None:
    conn.execute(
        text("INSERT INTO user_oauth_providers (user_id, provider, oauth_sub) VALUES (:u, :p, :s)"),
        {"u": user_id, "p": provider, "s": sub},
    )


def _oauth(conn: Connection, user_id: str, **kwargs) -> str:
    _user(conn, user_id, **kwargs)
    _provider(conn, user_id, "google", user_id)
    return user_id


def _allow(
    conn: Connection,
    provider: str,
    subject_id: str,
    source: str,
    *,
    offset: str = "-1 day",
    added_by: str | None = None,
) -> uuid.UUID:
    entry_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO signup_allowlist (id, provider, subject_id, subject_label, "
            "github_user_id, github_username, source, state, added_by_user_id, created_at) "
            "VALUES (:id, :p, :s, 'label', :legacy, 'label', :source, 'active', :by, "
            "now() + CAST(:offset AS interval))"
        ),
        {
            "id": entry_id,
            "p": provider,
            "s": subject_id,
            "legacy": f"{provider}:{subject_id}"[:64],
            "source": source,
            "by": added_by,
            "offset": offset,
        },
    )
    return entry_id


def _invite(conn: Connection, inviter: str, entry_id: uuid.UUID | None) -> uuid.UUID:
    invite_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO beta_invites (id, token_hash, inviter_user_id, expires_at, "
            "redeemed_at, redeemed_allowlist_entry_id, label) "
            "VALUES (:id, :hash, :inviter, now() + interval '7 days', now(), :entry, :label)"
        ),
        {
            "id": invite_id,
            "hash": uuid.uuid4().hex + uuid.uuid4().hex,
            "inviter": inviter,
            "entry": entry_id,
            "label": LABEL,
        },
    )
    return invite_id


def _audit(
    conn: Connection, user_id: str, action: str, resource: str, metadata: dict | None = None
) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_logs (user_email, user_id, action, resource, user_metadata, "
            "created_at) VALUES ('actor', :u, :a, :r, CAST(:m AS json), now())"
        ),
        {"u": user_id, "a": action, "r": resource, "m": json.dumps(metadata or {})},
    )


def _record(conn: Connection, user_id: str) -> tuple:
    return tuple(
        conn.execute(
            text(
                "SELECT admitted_via, admitted_beta_invite_id, admitted_invite_issuer, "
                "admitted_by_user_id FROM users WHERE user_id = :u"
            ),
            {"u": user_id},
        ).one()
    )


@contextmanager
def _captured_logs() -> Iterator[list[logging.LogRecord]]:
    """Every record any logger handles while the migration runs.

    ``alembic/env.py`` re-runs ``fileConfig``, which resets handlers (caplog's
    included), so the records are taken where every logger hands them on.
    """
    records: list[logging.LogRecord] = []
    original = logging.Logger.handle

    def spy(self: logging.Logger, record: logging.LogRecord) -> None:
        records.append(record)
        original(self, record)

    with patch.object(logging.Logger, "handle", spy):
        yield records


class TestF45AdmissionBackfill:
    def test_every_kind_and_conflict_counts_only_and_round_trip(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_REV)
            engine = _sync_engine()
            ids: dict[str, str] = {}
            invites: dict[str, uuid.UUID] = {}
            with engine.begin() as conn:
                # The lowest row with is_initial_admin: the strict first user.
                ids["first"] = _oauth(conn, "g-first", role="admin", is_initial_admin=True)
                # A CLI admin, initial too: admin_cli wins.
                ids["cli"] = _user(
                    conn,
                    "local:root",
                    role="admin",
                    login_id="root",
                    is_initial_admin=True,
                    auth_method="password",
                )
                # is_initial_admin but not the lowest row: no evidence.
                ids["late_initial"] = _oauth(conn, "g-late", role="admin", is_initial_admin=True)
                ids["inviter"] = _oauth(conn, "g-inviter")
                ids["admin"] = _oauth(conn, "g-admin", role="admin")

                for admission in (
                    "first_user",
                    "open_registration",
                    "workspace_invitation",
                    "beta_invite",
                ):
                    uid = _user(conn, f"email:{uuid.uuid4()}", auth_method="password")
                    ids[f"email_{admission}"] = uid
                    _audit(
                        conn,
                        uid,
                        "user.signed_up",
                        f"user:{uid}",
                        {"method": "email", "admission": admission, "role": "user"},
                    )
                email_beta = ids["email_beta_invite"]
                entry = _allow(conn, "email", email_beta, "beta_invite", added_by="ignored")
                invites["email_beta"] = _invite(conn, ids["inviter"], entry)

                # Beta invite through the creating identity's allowlist row.
                ids["beta_user"] = _oauth(conn, "g-beta-user")
                entry = _allow(conn, "google", "g-beta-user", "beta_invite", added_by="spoof")
                invites["beta_user"] = _invite(conn, ids["inviter"], entry)
                ids["beta_admin"] = _oauth(conn, "g-beta-admin")
                entry = _allow(conn, "google", "g-beta-admin", "beta_invite")
                invites["beta_admin"] = _invite(conn, ids["admin"], entry)
                # Allowlist row pruned: the audit row still names the invite.
                ids["beta_audit"] = _oauth(conn, "g-beta-audit")
                invites["beta_audit"] = _invite(conn, ids["inviter"], None)
                _audit(
                    conn,
                    "g-beta-audit",
                    "beta_invite.redeemed",
                    f"beta_invite:{invites['beta_audit']}",
                    {"provider": "google", "inviter_user_id": "spoofed-inviter"},
                )
                # The invite is gone (its inviter was erased): details stay NULL.
                ids["beta_gone"] = _oauth(conn, "g-beta-gone")
                _allow(conn, "google", "g-beta-gone", "beta_invite")

                ids["manual_before"] = _oauth(conn, "g-manual-before")
                _allow(conn, "google", "g-manual-before", "manual", offset="-1 day")
                ids["manual_after"] = _oauth(conn, "g-manual-after")
                _allow(conn, "google", "g-manual-after", "manual", offset="1 day")
                # A linked identity's beta_invite row is not the creating identity's.
                ids["linked"] = _oauth(conn, "g-linked")
                _provider(conn, "g-linked", "github", "gh-linked")
                _allow(conn, "github", "gh-linked", "beta_invite")

                ids["invited"] = _oauth(conn, "g-invited")
                _audit(conn, "g-invited", "signup_admitted", "signup_gate:google")
                ids["nothing"] = _oauth(conn, "g-nothing")

                # Conflicts.
                ids["manual_and_invited"] = _oauth(conn, "g-manual-invited")
                _allow(conn, "google", "g-manual-invited", "manual", offset="-1 day")
                _audit(conn, "g-manual-invited", "signup_admitted", "signup_gate:google")
                ids["beta_and_manual"] = _oauth(conn, "g-beta-manual")
                _allow(conn, "google", "g-beta-manual", "manual", offset="-1 day")
                entry = _allow(conn, "google", "g-beta-manual", "beta_invite")
                invites["beta_and_manual"] = _invite(conn, ids["inviter"], entry)

            with _captured_logs() as records, _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), REV)

            inviter = ids["inviter"]
            expected = {
                "first": ("first_user", None, None, None),
                "cli": ("admin_cli", None, None, None),
                "late_initial": ("unknown", None, None, None),
                "inviter": ("unknown", None, None, None),
                "admin": ("unknown", None, None, None),
                "email_first_user": ("first_user", None, None, None),
                "email_open_registration": ("open_registration", None, None, None),
                "email_workspace_invitation": ("workspace_invitation", None, None, None),
                "email_beta_invite": ("beta_invite", invites["email_beta"], "user", inviter),
                "beta_user": ("beta_invite", invites["beta_user"], "user", inviter),
                "beta_admin": ("beta_invite", invites["beta_admin"], "admin", None),
                "beta_audit": ("beta_invite", invites["beta_audit"], "user", inviter),
                "beta_gone": ("beta_invite", None, None, None),
                "manual_before": ("allowlist", None, None, None),
                "manual_after": ("unknown", None, None, None),
                "linked": ("unknown", None, None, None),
                "invited": ("workspace_invitation", None, None, None),
                "nothing": ("unknown", None, None, None),
                "manual_and_invited": ("allowlist", None, None, None),
                "beta_and_manual": ("beta_invite", invites["beta_and_manual"], "user", inviter),
            }
            with engine.begin() as conn:
                actual = {key: _record(conn, uid) for key, uid in ids.items()}
                assert actual == expected
                # Raw set-based UPDATEs: no row's updated_at moved.
                assert (
                    conn.execute(
                        text("SELECT count(*) FROM users WHERE updated_at IS NOT NULL")
                    ).scalar_one()
                    == 0
                )

            # Counts only, once.
            (line,) = [r.getMessage() for r in records if "admission backfill" in r.getMessage()]
            counts = json.loads(line.split("counts: ", 1)[1])
            assert counts == {
                "admin_cli": 1,
                "allowlist": 2,
                "beta_invite": 6,
                "beta_invite_details_filled": 5,
                "beta_invite_details_missing": 1,
                "first_user": 2,
                "open_registration": 1,
                "unknown": 6,
                "workspace_invitation": 2,
            }
            secrets = [*ids.values(), *(str(i) for i in invites.values()), LABEL, "f45.example"]
            for record in records:
                message = record.getMessage()
                assert not any(secret in message for secret in secrets), message

            self._check_schema(engine)

            # Deleting the inviter clears the link only.
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": inviter})
            with engine.begin() as conn:
                assert _record(conn, ids["beta_user"]) == (
                    "beta_invite",
                    invites["beta_user"],
                    "user",
                    None,
                )

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_REV)
            with engine.begin() as conn:
                columns = conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'users' AND column_name LIKE 'admitted%'"
                    )
                ).all()
                assert columns == []
                constraints = conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conname = ANY(:names) "
                        "OR conname = 'fk_users_admitted_by_user_id'"
                    ),
                    {"names": list(_CHECKS)},
                ).all()
                assert constraints == []

            # A re-upgrade rebuilds what the evidence still gives.
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), REV)
            with engine.begin() as conn:
                assert _record(conn, ids["beta_admin"]) == (
                    "beta_invite",
                    invites["beta_admin"],
                    "admin",
                    None,
                )
                assert _record(conn, ids["manual_before"])[0] == "allowlist"
            self._check_schema(engine)
        finally:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

    @staticmethod
    def _check_schema(engine) -> None:
        with engine.begin() as conn:
            nullable, default = conn.execute(
                text(
                    "SELECT is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = 'admitted_via'"
                )
            ).one()
            assert nullable == "NO"
            assert "'unknown'" in default
            found = {
                row[0]
                for row in conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE conname = ANY(:names)"),
                    {"names": list(_CHECKS)},
                )
            }
            assert found == set(_CHECKS)
            assert (
                conn.execute(
                    text(
                        "SELECT confdeltype FROM pg_constraint "
                        "WHERE conname = 'fk_users_admitted_by_user_id'"
                    )
                ).scalar_one()
                == "n"
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = :n"),
                    {"n": "ix_users_admitted_by_user_id"},
                ).scalar_one()
                == 1
            )
            # The CHECKs hold: invite fields on a non-invite row are refused.
            savepoint = conn.begin_nested()
            try:
                conn.execute(
                    text(
                        "UPDATE users SET admitted_invite_issuer = 'admin' "
                        "WHERE admitted_via = 'allowlist'"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                assert "admitted_invite_fields_beta_only" in str(exc)
            else:
                raise AssertionError("the CHECK did not refuse invite fields on allowlist")
            finally:
                if savepoint.is_active:
                    savepoint.rollback()
