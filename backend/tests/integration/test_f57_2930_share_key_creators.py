"""DB contract for the share-key creator migration f57 (#2930).

Share keys belong to the account that created them: deleting or erasing an
account deletes them. f57 applies that rule to the rows already stored:

- a share key whose ``user_id`` has no ``users`` row is deleted;
- a share key of an existing account stays, byte-identical;
- the log carries the number of rows deleted and nothing else;
- the downgrade changes nothing; a re-upgrade deletes nothing more, and
  deletes a row without a creator seeded in between.

PRE_REV is read from the module (its ``down_revision``), so the seed always
runs at f57's own parent. The test ends on an empty schema at head.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)
from tests.migration_ast import ALEMBIC_VERSIONS

if str(ALEMBIC_VERSIONS) not in sys.path:
    sys.path.insert(0, str(ALEMBIC_VERSIONS))

REV = "f57_2930_share_key_creators"
MODULE = importlib.import_module(REV)
PRE_REV: str = MODULE.down_revision
NAME = "SENTINEL-NAME-f57-do-not-log"
_LINE = re.compile(r"f57_2930 deleted share keys: (\d+)$")


def _user(conn: Connection, user_id: str) -> None:
    conn.execute(
        text(
            "INSERT INTO users (email, user_id, role, timezone, locale, is_initial_admin, "
            "auth_method) VALUES (:email, :uid, 'user', 'UTC', 'en', false, 'oauth')"
        ),
        {"email": f"{uuid.uuid4().hex}@f57.example", "uid": user_id},
    )


def _context(conn: Connection, owner: str) -> uuid.UUID:
    workspace_id, context_id = uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :owner, 'free')"
        ),
        {"id": workspace_id, "name": f"f57-{workspace_id.hex[:8]}", "owner": owner},
    )
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) "
            "VALUES (:id, :ws, :name, :owner)"
        ),
        {"id": context_id, "ws": workspace_id, "name": f"f57-{context_id.hex[:8]}", "owner": owner},
    )
    return context_id


def _share_key(conn: Connection, user_id: str, context_id: uuid.UUID) -> int:
    return conn.execute(
        text(
            "INSERT INTO share_keys (key_hash, key_prefix, name, user_id, context_id, "
            "expires_at) VALUES (:hash, 'kagura_sk_f57', :name, :uid, :ctx, :exp) "
            "RETURNING id"
        ),
        {
            "hash": uuid.uuid4().hex + uuid.uuid4().hex,
            "name": NAME,
            "uid": user_id,
            "ctx": context_id,
            "exp": datetime(2040, 1, 1) + timedelta(days=1),
        },
    ).scalar_one()


def _rows(conn: Connection) -> dict[int, tuple]:
    return {
        row[0]: tuple(row[1:])
        for row in conn.execute(
            text(
                "SELECT id, public_id, key_hash, name, user_id, context_id, expires_at, "
                "revoked_at, last_used_at FROM share_keys"
            )
        )
    }


@contextmanager
def _captured_logs() -> Iterator[list[logging.LogRecord]]:
    """Every record any logger handles while the migration runs (see f49's test)."""
    records: list[logging.LogRecord] = []
    original = logging.Logger.handle

    def spy(self: logging.Logger, record: logging.LogRecord) -> None:
        records.append(record)
        original(self, record)

    with patch.object(logging.Logger, "handle", spy):
        yield records


def _deleted(records: list[logging.LogRecord]) -> int:
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


def test_f57_emits_its_statement_offline_without_reading_a_result() -> None:
    """``alembic upgrade --sql``: the bind's ``execute`` returns nothing; the
    statement is emitted and no count is logged."""
    bind = MagicMock()
    bind.execute.return_value = None
    with patch.object(MODULE, "op") as op, _captured_logs() as records:
        op.get_bind.return_value = bind
        MODULE.upgrade()
    (call,) = bind.execute.call_args_list
    assert str(call.args[0]) == MODULE.DELETE_SHARE_KEYS_WITHOUT_CREATOR
    assert not any(_LINE.search(record.getMessage()) for record in records)


def test_f57_follows_the_slot_tables_revision() -> None:
    assert PRE_REV == "f56_2918_workspace_slots"


def test_f57_deletes_share_keys_without_a_creator_and_keeps_the_rest() -> None:
    _reset_alembic_state()
    try:
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_REV)
        engine = _sync_engine()
        kept_user = f"f57-kept-{uuid.uuid4().hex[:8]}"
        gone_numeric = "123456789012"
        gone_email = f"email:{uuid.uuid4()}"
        with engine.begin() as conn:
            _user(conn, kept_user)
            ctx = _context(conn, kept_user)
            kept = _share_key(conn, kept_user, ctx)
            orphans = [_share_key(conn, gone_numeric, ctx), _share_key(conn, gone_email, ctx)]
            before = _rows(conn)

        records = _upgrade()

        with engine.begin() as conn:
            after = _rows(conn)
        assert set(after) == {kept}
        assert after[kept] == before[kept]
        assert not set(orphans) & set(after)
        # One line with the count; no id, name or account in any log line.
        assert _deleted(records) == 2
        secrets = [kept_user, gone_numeric, gone_email, NAME, "f57.example", str(ctx)]
        for record in records:
            message = record.getMessage()
            assert not any(secret in message for secret in secrets), message

        # The downgrade changes nothing; a re-upgrade deletes nothing more.
        _downgrade()
        with engine.begin() as conn:
            assert _rows(conn) == after
        assert _deleted(_upgrade()) == 0
        with engine.begin() as conn:
            assert _rows(conn) == after

        # A row without a creator seeded before a further re-upgrade is deleted.
        _downgrade()
        with engine.begin() as conn:
            late = _share_key(conn, f"gone-{uuid.uuid4().hex[:8]}", ctx)
        assert _deleted(_upgrade()) == 1
        with engine.begin() as conn:
            final = _rows(conn)
        assert late not in final
        assert final == after
    finally:
        # Leave an empty schema at head, as the test found it.
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
