"""DB contract for the allowlist-adder cleanup migration f49 (#2817).

From v0.162.0 erasing or deleting an account clears it from
``signup_allowlist.added_by_user_id``. Rows whose adder was removed before
that still hold the person's user id, and no later removal can clear it (no
``users`` row is left). f49 sets such values to NULL once:

- an adder with no ``users`` row (an ``email:<uuid>`` id and a provider's
  numeric subject) reads NULL after the upgrade;
- an adder that still has a ``users`` row keeps its value;
- an ``operator:`` principal (``SYSTEM_ADMIN_MODE=service``, no ``users`` row)
  keeps its value;
- a NULL stays NULL, every row stays, no row's ``updated_at`` moves.

The log carries the number of rows changed and nothing else (no id or
label). The downgrade changes nothing; a re-upgrade changes nothing more and
still clears an orphan seeded in between.

PRE_REV is read from the module (its ``down_revision``), so the seed always
runs at f49's own parent. The test ends on an empty schema at head.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from auth.system_admin_mode import OPERATOR_PRINCIPAL_PREFIX
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)
from tests.migration_ast import ALEMBIC_VERSIONS

if str(ALEMBIC_VERSIONS) not in sys.path:
    sys.path.insert(0, str(ALEMBIC_VERSIONS))

REV = "f49_2817_clear_allowlist_adders"
MODULE = importlib.import_module(REV)
PRE_REV: str = MODULE.down_revision
LABEL = "SENTINEL-LABEL-f49-do-not-log"
SEEDED_AT = datetime(2020, 1, 2, 3, 4, 5)
_LINE = re.compile(r"f49_2817 cleared allowlist adders: (\d+)$")


def _user(conn: Connection, user_id: str) -> str:
    conn.execute(
        text(
            "INSERT INTO users (email, user_id, role, timezone, locale, is_initial_admin, "
            "auth_method) VALUES (:email, :uid, 'user', 'UTC', 'en', false, 'oauth')"
        ),
        {"email": f"{uuid.uuid4().hex}@f49.example", "uid": user_id},
    )
    return user_id


def _allow(conn: Connection, added_by: str | None, source: str = "manual") -> uuid.UUID:
    entry_id = uuid.uuid4()
    subject = f"subject-{uuid.uuid4().hex}"
    conn.execute(
        text(
            "INSERT INTO signup_allowlist (id, provider, subject_id, subject_label, "
            "github_user_id, github_username, source, state, added_by_user_id, "
            "created_at, updated_at) "
            "VALUES (:id, 'github', :s, :label, :legacy, :label, :source, 'active', :by, "
            ":at, :at)"
        ),
        {
            "id": entry_id,
            "s": subject,
            "label": LABEL,
            "legacy": subject[:64],
            "source": source,
            "by": added_by,
            "at": SEEDED_AT,
        },
    )
    return entry_id


def _rows(conn: Connection) -> dict[uuid.UUID, tuple[str | None, datetime]]:
    return {
        row_id: (added_by, updated_at)
        for row_id, added_by, updated_at in conn.execute(
            text("SELECT id, added_by_user_id, updated_at FROM signup_allowlist")
        )
    }


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


def _cleared(records: list[logging.LogRecord]) -> int:
    (count,) = [
        int(match.group(1))
        for record in records
        if (match := _LINE.search(record.getMessage())) is not None
    ]
    return count


def _upgrade() -> list[logging.LogRecord]:
    with _captured_logs() as records, _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), REV)
    return records


def _downgrade() -> None:
    with _alembic_at_test_db():
        command.downgrade(_get_alembic_config(), PRE_REV)


class TestF49ClearAllowlistAdders:
    def test_the_prefix_literal_is_the_operator_principal_prefix(self) -> None:
        # A revision stays frozen, so it writes the prefix out; it must be the
        # one the service-mode operator principal carries.
        assert MODULE.OPERATOR_PREFIX == OPERATOR_PRINCIPAL_PREFIX
        assert f"NOT LIKE '{OPERATOR_PRINCIPAL_PREFIX}%'" in MODULE.CLEAR_ORPHANED_ADDERS

    def test_orphans_cleared_kept_values_kept_counts_only_and_rerun(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_REV)
            engine = _sync_engine()
            orphan_email = f"email:{uuid.uuid4()}"
            orphan_numeric = "987654321"
            kept_user = "g-kept-adder"
            operator = f"{OPERATOR_PRINCIPAL_PREFIX}ops@example.com"
            with engine.begin() as conn:
                _user(conn, kept_user)
                ids = {
                    "orphan_email": _allow(conn, orphan_email),
                    "orphan_numeric": _allow(conn, orphan_numeric, source="beta_invite"),
                    "kept": _allow(conn, kept_user, source="beta_invite"),
                    "operator": _allow(conn, operator),
                    "null": _allow(conn, None),
                }

            records = _upgrade()

            expected = {
                "orphan_email": None,
                "orphan_numeric": None,
                "kept": kept_user,
                "operator": operator,
                "null": None,
            }
            with engine.begin() as conn:
                rows = _rows(conn)
            # Every row is still there, and only these.
            assert set(rows) == set(ids.values())
            assert {key: rows[row_id][0] for key, row_id in ids.items()} == expected
            # Raw UPDATE: no row's updated_at moved.
            assert {updated_at for _, updated_at in rows.values()} == {SEEDED_AT}

            # One line with the count, and no seeded id or label anywhere.
            assert _cleared(records) == 2
            secrets = [orphan_email, orphan_numeric, kept_user, operator, LABEL, "f49.example"]
            secrets += [str(row_id) for row_id in ids.values()]
            for record in records:
                message = record.getMessage()
                assert not any(secret in message for secret in secrets), message

            # The downgrade changes nothing (the ids are not kept anywhere).
            _downgrade()
            with engine.begin() as conn:
                assert _rows(conn) == rows

            # Upgrade again: nothing left to clear, kept values still kept.
            assert _cleared(_upgrade()) == 0
            with engine.begin() as conn:
                assert _rows(conn) == rows

            # An orphan seeded before a further re-upgrade is cleared.
            _downgrade()
            with engine.begin() as conn:
                late = _allow(conn, f"email:{uuid.uuid4()}")
            assert _cleared(_upgrade()) == 1
            with engine.begin() as conn:
                after = _rows(conn)
            assert after[late] == (None, SEEDED_AT)
            assert after[ids["kept"]][0] == kept_user
            assert after[ids["operator"]][0] == operator
        finally:
            # Leave an empty schema at head, as the test found it.
            _reset_alembic_state()
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")
