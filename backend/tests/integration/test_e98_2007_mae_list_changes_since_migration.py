"""Migration round-trip for e98_2007_mae_list_changes_since (#2007).

1. upgrade widens ``valid_mae_operation`` with ``'list'`` and
   ``'changes_since'``, appended after ``'load_guardrails'`` and validated;
2. downgrade with no such rows restores the previous CHECK fully validated;
3. downgrade with a ``'list'`` audit row present keeps the row (append-only
   table) and leaves the restored CHECK ``NOT VALID``; the next upgrade
   validates it again.

The audit row is left in place for the same reason as in the e84 test: the
next run's ``_reset_alembic_state()`` drops the schema with it.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

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

E98_REVISION = "e98_2007_mae_list_changes_since"
PRIOR_HEAD = "e97_1975_memories_per_day_addon"


def _mae_check(conn: Connection) -> str:
    return conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'valid_mae_operation'"
        )
    ).scalar_one()


def _mae_check_validated(conn: Connection) -> bool:
    return conn.execute(
        text("SELECT convalidated FROM pg_constraint WHERE conname = 'valid_mae_operation'")
    ).scalar_one()


def _upgrade(rev: str) -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), rev)


def _downgrade(rev: str) -> None:
    with _alembic_at_test_db():
        command.downgrade(_get_alembic_config(), rev)


def test_e98_round_trip_check() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    audit_user = f"e98-migration-test-{uuid.uuid4().hex[:8]}"
    try:
        _upgrade(PRIOR_HEAD)
        with engine.connect() as conn:
            check = _mae_check(conn)
            assert "'list'" not in check and "'changes_since'" not in check
            assert "'load_guardrails'" in check

        _upgrade(E98_REVISION)
        with engine.connect() as conn:
            check = _mae_check(conn)
            # Appended, never reordered.
            assert (
                check.index("'load_guardrails'")
                < check.index("'list'")
                < check.index("'changes_since'")
            )
            assert _mae_check_validated(conn) is True

        _downgrade(PRIOR_HEAD)
        with engine.connect() as conn:
            check = _mae_check(conn)
            assert "'list'" not in check and "'changes_since'" not in check
            assert _mae_check_validated(conn) is True

        _upgrade(E98_REVISION)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO memory_access_events "
                    "(workspace_id, user_id, principal_type, surface, operation, outcome) "
                    "VALUES (:ws, :uid, 'oauth', 'mcp', 'list', 'denied')"
                ),
                {"ws": str(uuid.uuid4()), "uid": audit_user},
            )
            conn.commit()

        _downgrade(PRIOR_HEAD)
        with engine.connect() as conn:
            kept = conn.execute(
                text("SELECT count(*) FROM memory_access_events WHERE user_id = :u"),
                {"u": audit_user},
            ).scalar_one()
            assert kept == 1, "downgrade must keep audit rows"
            assert "'list'" not in _mae_check(conn)
            assert _mae_check_validated(conn) is False

        _upgrade(E98_REVISION)
        with engine.connect() as conn:
            assert "'changes_since'" in _mae_check(conn)
            assert _mae_check_validated(conn) is True
    finally:
        try:
            _upgrade("head")
        finally:
            engine.dispose()
