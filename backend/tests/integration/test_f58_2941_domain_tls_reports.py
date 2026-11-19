"""#2941 (f58): the edge certificate report table, created without touching an existing table.

The upgrade runs while another transaction holds ``ACCESS EXCLUSIVE`` on
``public_mcp_domains``, ``workspaces`` and ``users``: a revision that needed
any lock on them would wait into the migration ``lock_timeout`` and fail. A
rerun (the revision stamped back, the table still there) is a no-op;
downgrade drops the table and keeps every domain row.
"""

from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

BEFORE = "f56_2918_workspace_slots"
REVISION = "f58_2941_domain_tls_reports"
TABLE = "public_mcp_domain_tls_reports"

_INSERT = (
    "INSERT INTO public_mcp_domain_tls_reports "
    "(domain_id, endpoint_id, hostname, status, observed_at, valid_until, "
    "certificate_not_after, received_at) "
    "VALUES (:id, :endpoint, 'mcp.example.org', :status, now(), now() + interval '1 minute', "
    "NULL, now())"
)


def _version(engine) -> str:
    with engine.connect() as conn:
        return conn.scalar(text("SELECT version_num FROM alembic_version"))


def test_report_table_round_trips_without_locking_existing_tables():
    engine = _sync_engine()
    holder = _sync_engine()
    try:
        _reset_alembic_state()
        _upgrade_to(BEFORE)
        assert TABLE not in set(inspect(engine).get_table_names())

        # Upgrade while the domain, workspace and user tables are locked against everything.
        with holder.connect() as lock_conn:
            lock_tx = lock_conn.begin()
            lock_conn.execute(
                text("LOCK TABLE public_mcp_domains, workspaces, users IN ACCESS EXCLUSIVE MODE")
            )
            try:
                _upgrade_to(REVISION)
            finally:
                lock_tx.rollback()
        assert _version(engine) == REVISION

        columns = {
            c["name"]: (str(c["type"]), c["nullable"]) for c in inspect(engine).get_columns(TABLE)
        }
        assert columns == {
            "domain_id": ("UUID", False),
            "endpoint_id": ("UUID", False),
            "hostname": ("VARCHAR(255)", False),
            "status": ("VARCHAR(32)", False),
            "observed_at": ("TIMESTAMP", False),
            "valid_until": ("TIMESTAMP", False),
            "certificate_not_after": ("TIMESTAMP", True),
            "received_at": ("TIMESTAMP", False),
        }
        timezones = {
            c["name"]: getattr(c["type"], "timezone", None)
            for c in inspect(engine).get_columns(TABLE)
            if str(c["type"]) == "TIMESTAMP"
        }
        assert set(timezones.values()) == {True}
        assert inspect(engine).get_pk_constraint(TABLE)["constrained_columns"] == ["domain_id"]
        assert [i["name"] for i in inspect(engine).get_indexes(TABLE)] == [
            "ix_public_mcp_domain_tls_reports_valid_until"
        ]
        assert not inspect(engine).get_foreign_keys(TABLE)
        # No existing table changed: the domain table has no report column.
        assert "tls_report" not in {
            c["name"] for c in inspect(engine).get_columns("public_mcp_domains")
        }

        with engine.begin() as db:
            db.execute(text(_INSERT), {"id": uuid4(), "endpoint": uuid4(), "status": "valid"})
        with pytest.raises(IntegrityError) as rejected, engine.begin() as db:
            db.execute(text(_INSERT), {"id": uuid4(), "endpoint": uuid4(), "status": "revoked"})
        assert (
            getattr(rejected.value.orig, "sqlstate", None)
            or getattr(rejected.value.orig, "pgcode", None)
        ) == "23514"

        # A rerun after an interrupted upgrade: the table exists, the revision is
        # not recorded. The upgrade is a no-op and keeps the row.
        with engine.begin() as db:
            db.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        assert _version(engine) == REVISION
        with engine.connect() as db:
            assert db.scalar(text("SELECT count(*) FROM public_mcp_domain_tls_reports")) == 1

        _downgrade_to(BEFORE)
        assert TABLE not in set(inspect(engine).get_table_names())
        _upgrade_to(REVISION)
        assert TABLE in set(inspect(engine).get_table_names())
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
            holder.dispose()
