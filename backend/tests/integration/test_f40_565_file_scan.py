"""DB contract for f40 (#565): malware scan state on ``file_objects``.

- upgrade adds the scan columns; existing rows become ``unscanned`` (no
  backfill UPDATE) and ``status`` is untouched;
- both CHECKs are enforced and validated;
- the three partial indexes exist and are valid;
- an interrupted upgrade (an index left INVALID, another missing, columns
  already there) is completed by running it again;
- downgrade is refused while a live row is ``infected`` or ``error`` and
  drops everything once there is none; the round trip is repeatable.

The revision before f40 is read from the migration module itself, so
re-chaining f40 onto another parent changes one line in the migration and
nothing here. Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import importlib.util
import uuid
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
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "f40_565_file_scan.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("_f40_565_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _migration_module()
F40_REV = _MODULE.revision
PRIOR_REV = _MODULE.down_revision

_SCAN_COLUMNS = {
    "scan_status",
    "scan_attempts",
    "scan_next_attempt_at",
    "scan_lease_until",
    "scanned_at",
    "scan_signature",
    "scan_etag",
    "scan_last_modified",
    "scan_error",
}
_INDEXES = (
    "idx_file_objects_scan_queue",
    "idx_file_objects_scan_backfill",
    "idx_file_objects_quarantine",
)


def _seed_workspace(conn: Connection) -> str:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"u-{suffix}"
    workspace_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO users (email, user_id, role, timezone, locale, is_initial_admin)"
            " VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{user_id}@test.example", "uid": user_id},
    )
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": workspace_id, "name": f"ws-{suffix}", "owner": user_id},
    )
    return workspace_id


def _insert_file(conn: Connection, workspace_id: str, **scan: str) -> str:
    sha = uuid.uuid4().hex + uuid.uuid4().hex
    columns = ", ".join(scan)
    values = ", ".join(f":{name}" for name in scan)
    return str(
        conn.execute(
            text(
                "INSERT INTO file_objects (workspace_id, sha256, size_bytes, content_type,"
                " filename, storage_backend, storage_key, status, created_by, uploaded_at"
                + (f", {columns}" if scan else "")
                + ") VALUES (:ws, :sha, 5, 'text/plain', 'a.txt', 'r2', :key, 'uploaded',"
                " 'u-test', now()" + (f", {values}" if scan else "") + ") RETURNING id"
            ),
            {"ws": workspace_id, "sha": sha, "key": f"{workspace_id}/{sha[:2]}/{sha}", **scan},
        ).scalar_one()
    )


def _columns(conn: Connection) -> set[str]:
    return set(
        conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'file_objects' AND column_name LIKE 'scan%'"
            )
        ).scalars()
    )


def _index_validity(conn: Connection) -> dict[str, bool]:
    rows = conn.execute(
        text(
            "SELECT c.relname, i.indisvalid FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relname = ANY(:names)"
        ),
        {"names": list(_INDEXES)},
    ).all()
    return dict(rows)


def _upgrade(rev: str) -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), rev)


def _downgrade(rev: str) -> None:
    with _alembic_at_test_db():
        command.downgrade(_get_alembic_config(), rev)


class TestF40FileScan:
    def test_revision_ids(self) -> None:
        assert F40_REV == "f40_565_file_scan"
        assert len(F40_REV) <= 32
        assert PRIOR_REV

    def test_upgrade_rerun_downgrade_round_trip(self) -> None:
        _reset_alembic_state()
        try:
            _upgrade(PRIOR_REV)
            engine = _sync_engine()
            with engine.begin() as conn:
                assert _columns(conn) == set()
                ws = _seed_workspace(conn)
                legacy = _insert_file(conn, ws)

            _upgrade(F40_REV)
            with engine.begin() as conn:
                assert _columns(conn) == _SCAN_COLUMNS
                row = conn.execute(
                    text(
                        "SELECT status, scan_status, scan_attempts, scan_error"
                        " FROM file_objects WHERE id = :id"
                    ),
                    {"id": legacy},
                ).one()
                assert tuple(row) == ("uploaded", "unscanned", 0, None)
                assert _index_validity(conn) == dict.fromkeys(_INDEXES, True)
                queue = conn.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                    {"n": "idx_file_objects_scan_queue"},
                ).scalar_one()
                assert "scan_next_attempt_at" in queue and "'pending'" in queue
                validated = dict(
                    conn.execute(
                        text(
                            "SELECT conname, convalidated FROM pg_constraint"
                            " WHERE conname IN ('valid_file_scan_status', 'valid_file_scan_error')"
                        )
                    ).all()
                )
                assert validated == {"valid_file_scan_status": True, "valid_file_scan_error": True}

            with pytest.raises(IntegrityError, match="valid_file_scan_status"):
                with engine.begin() as conn:
                    _insert_file(conn, ws, scan_status="quarantined")
            with pytest.raises(IntegrityError, match="valid_file_scan_error"):
                with engine.begin() as conn:
                    _insert_file(conn, ws, scan_status="error", scan_error="other")

            # An interrupted upgrade: one index INVALID, one missing, the
            # version still at the prior revision. Running it again finishes it.
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE pg_index SET indisvalid = false WHERE indexrelid ="
                        " 'idx_file_objects_scan_queue'::regclass"
                    )
                )
                conn.execute(text("DROP INDEX idx_file_objects_quarantine"))
            with _alembic_at_test_db():
                command.stamp(_get_alembic_config(), PRIOR_REV)
            _upgrade(F40_REV)
            with engine.begin() as conn:
                assert _index_validity(conn) == dict.fromkeys(_INDEXES, True)

            # A live blocked row refuses the downgrade (it would become
            # downloadable); a soft-deleted one does not count.
            with engine.begin() as conn:
                infected = _insert_file(conn, ws, scan_status="infected")
                error = _insert_file(conn, ws, scan_status="error", scan_error="scan_failed")
                gone = _insert_file(conn, ws, scan_status="infected")
                conn.execute(
                    text("UPDATE file_objects SET deleted_at = now() WHERE id = :id"),
                    {"id": gone},
                )
            for blocked in (infected, error):
                with pytest.raises(RuntimeError, match="downgrade refused"):
                    _downgrade(PRIOR_REV)
                with engine.begin() as conn:
                    assert _columns(conn) == _SCAN_COLUMNS  # nothing was dropped
                    conn.execute(text("DELETE FROM file_objects WHERE id = :id"), {"id": blocked})

            _downgrade(PRIOR_REV)
            with engine.begin() as conn:
                assert _columns(conn) == set()
                assert _index_validity(conn) == {}
                assert (
                    conn.execute(
                        text("SELECT status FROM file_objects WHERE id = :id"), {"id": legacy}
                    ).scalar_one()
                    == "uploaded"
                )

            # Repeatable.
            _upgrade(F40_REV)
            with engine.begin() as conn:
                assert _columns(conn) == _SCAN_COLUMNS
                conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": ws})
        finally:
            _upgrade("head")
