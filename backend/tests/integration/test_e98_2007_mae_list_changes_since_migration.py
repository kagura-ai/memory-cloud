"""Migration round-trip for e98_2007_mae_list_changes_since (#2007).

1. upgrade widens ``valid_mae_operation`` with ``'list'``,
   ``'changes_since'``, ``'recall_upcoming'`` and ``'recall_nearby'``,
   appended after ``'load_guardrails'`` and validated;
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

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _mae_check,
    _mae_check_validated,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

E98_REVISION = "e98_2007_mae_list_changes_since"
PRIOR_HEAD = "e97_1975_memories_per_day_addon"


def test_e98_round_trip_check() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    audit_user = f"e98-migration-test-{uuid.uuid4().hex[:8]}"
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            check = _mae_check(conn)
            assert "'list'" not in check and "'changes_since'" not in check
            assert "'load_guardrails'" in check

        _upgrade_to(E98_REVISION)
        with engine.connect() as conn:
            check = _mae_check(conn)
            # Appended, never reordered.
            assert (
                check.index("'load_guardrails'")
                < check.index("'list'")
                < check.index("'changes_since'")
                < check.index("'recall_upcoming'")
                < check.index("'recall_nearby'")
            )
            assert _mae_check_validated(conn) is True

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            check = _mae_check(conn)
            assert "'list'" not in check and "'recall_nearby'" not in check
            assert _mae_check_validated(conn) is True

        _upgrade_to(E98_REVISION)
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

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            kept = conn.execute(
                text("SELECT count(*) FROM memory_access_events WHERE user_id = :u"),
                {"u": audit_user},
            ).scalar_one()
            assert kept == 1, "downgrade must keep audit rows"
            assert "'list'" not in _mae_check(conn)
            assert _mae_check_validated(conn) is False

        _upgrade_to(E98_REVISION)
        with engine.connect() as conn:
            assert "'changes_since'" in _mae_check(conn)
            assert _mae_check_validated(conn) is True
    finally:
        try:
            _upgrade_to("head")
        finally:
            engine.dispose()
