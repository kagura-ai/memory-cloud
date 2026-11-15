"""Upgrade either f51 branch and unmerge without losing fences or domain claims."""

from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from alembic import command
from models.sync_management import SyncManagedTarget, SyncModeOperation
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)
from tests.integration.test_f50_2807_merge_sync_heads import _assert_preserved, _seed_common

COMMON = "f50_2807_merge_sync_heads"
MANAGED = "f51_2808_managed_sync"
DOMAINS = "f51_2809_public_mcp_domains"
MERGE = "f52_2808_merge_feed_domains"
_TABLES = ("sync_managed_targets", "sync_mode_operations", "public_mcp_domains")


def _migrate(operation, revision):
    with _alembic_at_test_db():
        operation(_get_alembic_config(), revision)


def _seed_managed(engine, ids):
    with Session(engine) as db, db.begin():
        db.add(
            SyncManagedTarget(
                installation_id=ids.iid,
                resource_pk=ids.rid,
                context_id=ids.cid,
                mode="stopping",
                epoch=7,
                generation=1,
                stop_event_id=ids.event_id,
                drain_generation=1,
                stop_reason="human",
            )
        )
        db.add(
            SyncModeOperation(
                installation_id=ids.iid,
                operation_id=uuid4(),
                action="stop",
                request_hash="8" * 64,
                actor_user_id=ids.uid,
                result={"mode": "stopping", "epoch": 7},
            )
        )


def _seed_domain(engine, ids):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO public_mcp_domains (hostname, workspace_id, endpoint_id, "
                "state, verification_token, serve_until, verified_at, proof_ok_at) "
                "VALUES (:host, :wid, :eid, 'active', :token, "
                "now() + interval '72 hours', now(), now())"
            ),
            {
                "host": f"retained-{uuid4().hex[:8]}.example.org",
                "wid": ids.wid,
                "eid": ids.iid,
                "token": "9" * 64,
            },
        )


def _snapshot(engine, tables=_TABLES):
    with engine.connect() as conn:
        return {
            table: list(conn.scalars(text(f"SELECT to_jsonb(t) FROM {table} AS t")))
            for table in _TABLES
            if table in tables
        }


@pytest.mark.parametrize("starting_heads", [(COMMON,), (MANAGED,), (DOMAINS,), (MANAGED, DOMAINS)])
def test_upgrade_and_unmerge_retain_managed_state_and_domain_claims(async_engine, starting_heads):
    engine = _sync_engine()
    try:
        current_head = ScriptDirectory.from_config(_get_alembic_config()).get_current_head()
        assert current_head is not None
        _reset_alembic_state()
        for revision in starting_heads:
            _migrate(command.upgrade, revision)
        ids = _seed_common(engine)
        tables = set(inspect(engine).get_table_names())
        had_managed = "sync_managed_targets" in tables
        had_domain = "public_mcp_domains" in tables
        if had_managed:
            _seed_managed(engine, ids)
        if had_domain:
            _seed_domain(engine, ids)
        before = _snapshot(engine, tables)
        _migrate(command.upgrade, "head")
        after = _snapshot(engine)
        assert all(after[table] == rows for table, rows in before.items())
        if not had_managed:
            _seed_managed(engine, ids)
        if not had_domain:
            _seed_domain(engine, ids)
        expected = _snapshot(engine)
        for parent in (MANAGED, DOMAINS):
            _migrate(command.downgrade, MERGE)
            _migrate(command.downgrade, parent)
            with engine.connect() as conn:
                assert set(conn.scalars(text("SELECT version_num FROM alembic_version"))) == {
                    MANAGED,
                    DOMAINS,
                }
            assert _snapshot(engine) == expected
            _assert_preserved(engine, ids, sync=False, budget=False)
            _migrate(command.upgrade, "head")
            with engine.connect() as conn:
                assert set(conn.scalars(text("SELECT version_num FROM alembic_version"))) == {
                    current_head
                }
            assert _snapshot(engine) == expected
    finally:
        try:
            _reset_alembic_state()
            _migrate(command.upgrade, "head")
        finally:
            engine.dispose()
