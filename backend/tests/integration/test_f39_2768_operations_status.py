"""DB contract for f39 (#2768): ``scheduled_job_status`` and ``operations_llm_lanes``.

- upgrade creates both tables with their CHECKs, server defaults and
  timezone-aware timestamps;
- the CHECKs refuse a bad trigger kind, outcome, lane, probe outcome, a
  negative failure count and an oversized ``last_counts``;
- downgrade drops both tables;
- the round trip is repeatable.

The revision before f39 is read from the migration module itself, so
re-chaining f39 (or chaining a later revision onto it) changes one line in the
migration and nothing here.

A file of its own, like the other ``test_<revision>_...`` files:
``_reset_alembic_state`` rebuilds the schema through every migration. Not
executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "f39_2768_operations_status.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("_f39_2768_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _migration_module()
F39_REV = _MODULE.revision
PRIOR_REV = _MODULE.down_revision

_TABLES = ("scheduled_job_status", "operations_llm_lanes")


def _exists(conn: Connection, table: str) -> bool:
    return conn.execute(text(f"SELECT to_regclass('public.{table}')")).scalar_one() is not None


def _insert_job(conn: Connection, job_id: str = "a_job", **overrides: object) -> None:
    values: dict[str, object] = {
        "job_id": job_id,
        "job_name": "A job",
        "trigger_kind": "interval",
        "expected_interval_seconds": 60,
        "last_outcome": None,
        "consecutive_failures": 0,
        "last_counts": None,
    }
    values.update(overrides)
    conn.execute(
        text(
            "INSERT INTO scheduled_job_status (job_id, job_name, trigger_kind,"
            " expected_interval_seconds, last_registered_at, registered_since, last_outcome,"
            " consecutive_failures, last_counts)"
            " VALUES (:job_id, :job_name, :trigger_kind, :expected_interval_seconds, now(),"
            " now(), :last_outcome, :consecutive_failures, CAST(:last_counts AS jsonb))"
        ),
        values,
    )


def _insert_lane(conn: Connection, lane: str = "embedding", **overrides: object) -> None:
    values: dict[str, object] = {"lane": lane, "last_probe_outcome": None}
    values.update(overrides)
    conn.execute(
        text(
            "INSERT INTO operations_llm_lanes (lane, provider, model, credential_present,"
            " snapshot_at, last_probe_outcome)"
            " VALUES (:lane, 'openai', 'some-model', true, now(), :last_probe_outcome)"
        ),
        values,
    )


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestF39OperationsStatus:
    def test_revision_ids(self) -> None:
        assert F39_REV == "f39_2768_operations_status"
        assert len(F39_REV) <= 32
        assert PRIOR_REV

    def test_upgrade_creates_the_tables_and_downgrade_drops_them(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRIOR_REV)
            engine = _sync_engine()
            with engine.begin() as conn:
                for table in _TABLES:
                    assert not _exists(conn, table)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), F39_REV)

            with engine.begin() as conn:
                for table in _TABLES:
                    assert _exists(conn, table)
                columns = dict(
                    conn.execute(
                        text(
                            "SELECT column_name, data_type FROM information_schema.columns"
                            " WHERE table_name = 'scheduled_job_status'"
                        )
                    ).all()
                )
                assert columns["last_counts"] == "jsonb"
                assert columns["last_registered_at"] == "timestamp with time zone"
                assert columns["consecutive_failures"] == "integer"
                _insert_job(conn, last_counts=json.dumps({"deleted": 3}))
                row = conn.execute(
                    text(
                        "SELECT first_registered_at, updated_at, consecutive_failures"
                        " FROM scheduled_job_status WHERE job_id = 'a_job'"
                    )
                ).one()
                assert row.first_registered_at.tzinfo is not None
                assert row.updated_at is not None and row.consecutive_failures == 0
                _insert_lane(conn)
                probe_failures = conn.execute(
                    text("SELECT probe_consecutive_failures FROM operations_llm_lanes")
                ).scalar_one()
                assert probe_failures == 0

            refused = [
                (
                    "valid_scheduled_job_trigger_kind",
                    lambda c: _insert_job(c, "b", trigger_kind="date"),
                ),
                (
                    "valid_scheduled_job_outcome",
                    lambda c: _insert_job(c, "b", last_outcome="skipped"),
                ),
                (
                    "valid_scheduled_job_failures",
                    lambda c: _insert_job(c, "b", consecutive_failures=-1),
                ),
                (
                    "valid_scheduled_job_interval",
                    lambda c: _insert_job(c, "b", expected_interval_seconds=0),
                ),
                (
                    "valid_scheduled_job_counts_size",
                    lambda c: _insert_job(
                        c, "b", last_counts=json.dumps({f"k{i}": "x" * 100 for i in range(30)})
                    ),
                ),
                ("valid_operations_llm_lane", lambda c: _insert_lane(c, "chat")),
                (
                    "valid_operations_llm_lane_probe_outcome",
                    lambda c: _insert_lane(c, "rerank", last_probe_outcome="missed"),
                ),
            ]
            for constraint, insert in refused:
                with pytest.raises(IntegrityError, match=constraint):
                    with engine.begin() as conn:
                        insert(conn)

            with engine.begin() as conn:
                for lane in (
                    "sleep_judge",
                    "supersede_verify",
                    "recall_selector",
                    "analysis",
                    "rerank",
                ):
                    _insert_lane(conn, lane)

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRIOR_REV)
            with engine.begin() as conn:
                for table in _TABLES:
                    assert not _exists(conn, table)

            # Repeatable.
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), F39_REV)
            with engine.begin() as conn:
                for table in _TABLES:
                    assert _exists(conn, table)
        finally:
            _leave_db_at_head()
