"""DB contract for f41 (#2768): the platform lane index on ``llm_call_log``.

- upgrade builds ``idx_llm_call_log_platform_lane`` on
  ``(caller, provider, model, occurred_at)`` of the platform-paid rows, valid;
- an interrupted build (the index left INVALID) is finished by running the
  upgrade again;
- downgrade drops it; the round trip is repeatable.

The revision before f41 is read from the migration module itself. Not
executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "f41_2768_llm_call_log_lane_idx.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("_f41_2768_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _migration_module()
F41_REV = _MODULE.revision
PRIOR_REV = _MODULE.down_revision
_INDEX = "idx_llm_call_log_platform_lane"


def _index(conn: Connection) -> tuple[bool, str] | None:
    row = conn.execute(
        text(
            "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n"
        ),
        {"n": _INDEX},
    ).first()
    return None if row is None else (bool(row[0]), str(row[1]))


def _upgrade(rev: str) -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), rev)


def _downgrade(rev: str) -> None:
    with _alembic_at_test_db():
        command.downgrade(_get_alembic_config(), rev)


class TestF41LlmCallLogLaneIndex:
    def test_revision_ids(self) -> None:
        assert F41_REV == "f41_2768_llm_call_log_lane_idx"
        assert len(F41_REV) <= 32
        assert PRIOR_REV == "f40_565_file_scan"

    def test_upgrade_rerun_downgrade_round_trip(self) -> None:
        _reset_alembic_state()
        try:
            _upgrade(PRIOR_REV)
            engine = _sync_engine()
            with engine.begin() as conn:
                assert _index(conn) is None

            _upgrade(F41_REV)
            with engine.begin() as conn:
                found = _index(conn)
                assert found is not None
                valid, definition = found
                assert valid
                assert "(caller, provider, model, occurred_at)" in definition
                assert "WHERE ((paid_by)::text = 'platform'::text)" in definition

            # An interrupted build: the index left INVALID, the version still
            # at the prior revision. Running the upgrade again finishes it.
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE pg_index SET indisvalid = false"
                        " WHERE indexrelid = 'idx_llm_call_log_platform_lane'::regclass"
                    )
                )
            with _alembic_at_test_db():
                command.stamp(_get_alembic_config(), PRIOR_REV)
            _upgrade(F41_REV)
            with engine.begin() as conn:
                found = _index(conn)
                assert found is not None and found[0]

            _downgrade(PRIOR_REV)
            with engine.begin() as conn:
                assert _index(conn) is None

            # Repeatable.
            _upgrade(F41_REV)
            with engine.begin() as conn:
                assert _index(conn) is not None
        finally:
            _upgrade("head")
