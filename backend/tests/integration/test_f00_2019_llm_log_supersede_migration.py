"""Migration round-trip for f00_2019_llm_log_supersede (#2019).

1. upgrade widens ``valid_llm_call_log_caller`` with ``'supersede_verify'``,
   appended after ``'admin'`` and validated;
2. downgrade with no such rows restores the previous CHECK fully validated;
3. downgrade with a ``'supersede_verify'`` ledger row present keeps the row
   (append-only cost ledger) and leaves the restored CHECK ``NOT VALID``; the
   next upgrade validates it again.

The ledger row is left in place for the same reason as in the e84 / e98
tests: the next run's ``_reset_alembic_state()`` drops the schema with it.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

F00_REVISION = "f00_2019_llm_log_supersede"
PRIOR_HEAD = "e99_2022_supersede_cand_null"


def _caller_check(conn) -> str:
    return conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'valid_llm_call_log_caller'"
        )
    ).scalar_one()


def _caller_check_validated(conn) -> bool:
    return conn.execute(
        text("SELECT convalidated FROM pg_constraint WHERE conname = 'valid_llm_call_log_caller'")
    ).scalar_one()


def test_f00_round_trip_check() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    ledger_user = f"f00-migration-test-{uuid.uuid4().hex[:8]}"
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            check = _caller_check(conn)
            assert "'supersede_verify'" not in check and "'admin'" in check

        _upgrade_to(F00_REVISION)
        with engine.connect() as conn:
            check = _caller_check(conn)
            # Appended, never reordered.
            assert check.index("'ask'") < check.index("'admin'") < check.index("'supersede_verify'")
            assert _caller_check_validated(conn) is True

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert "'supersede_verify'" not in _caller_check(conn)
            assert _caller_check_validated(conn) is True

        _upgrade_to(F00_REVISION)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_call_log "
                    "(occurred_at, user_id, workspace_id, context_id, caller, call_type, "
                    "provider, model, input_tokens, output_tokens) "
                    "VALUES (now(), :uid, :ws, :ctx, 'supersede_verify', 'completion', "
                    "'openai', 'gpt-5-nano', 10, 5)"
                ),
                {"uid": ledger_user, "ws": str(uuid.uuid4()), "ctx": str(uuid.uuid4())},
            )
            conn.commit()

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            kept = conn.execute(
                text("SELECT count(*) FROM llm_call_log WHERE user_id = :u"),
                {"u": ledger_user},
            ).scalar_one()
            assert kept == 1, "downgrade must keep ledger rows"
            assert "'supersede_verify'" not in _caller_check(conn)
            assert _caller_check_validated(conn) is False

        _upgrade_to(F00_REVISION)
        with engine.connect() as conn:
            assert "'supersede_verify'" in _caller_check(conn)
            assert _caller_check_validated(conn) is True
    finally:
        try:
            _upgrade_to("head")
        finally:
            engine.dispose()
