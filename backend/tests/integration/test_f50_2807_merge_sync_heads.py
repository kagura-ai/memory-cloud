"""Upgrade either deployed branch, retaining staged feeds and active usage periods."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.orm import Session

from alembic import command
from models.auth import Context, User, Workspace
from models.public_usage_budget import PublicUsageBudget
from models.resource import Resource, ResourceEvent
from models.sync_feed import SyncChunk, SyncFeed, SyncIndexOutbox, SyncRecord, SyncRun
from models.sync_installation import SyncInstallation, SyncService
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)
from utils.datetime import utcnow

MERGE = "f50_2807_merge_sync_heads"
SYNC = "f48_2807_sync_feed_runs"
BUDGET = "f48_2805_public_usage_budgets"
CLEAR = "f49_2817_clear_allowlist_adders"
COMMON = "f47_2819_merge_sync_credentials"


def _migrate(operation, revision):
    with _alembic_at_test_db():
        operation(_get_alembic_config(), revision)


def _seed_common(engine):
    ids = SimpleNamespace(
        uid="merge-" + uuid4().hex,
        sid="merge-" + uuid4().hex,
        wid=uuid4(),
        rid=uuid4(),
        cid=uuid4(),
        iid=uuid4(),
        fid=uuid4(),
        pending=uuid4(),
        committed=uuid4(),
        now=utcnow(),
    )
    with Session(engine) as db, db.begin():
        db.add(User(user_id=ids.uid, email=ids.uid + "@example.test"))
        db.flush()
        db.add(Workspace(id=ids.wid, name="Preserved workspace", owner_user_id=ids.uid))
        db.add(
            SyncService(
                service_id=ids.sid,
                browser_client_id=ids.sid + "-browser",
                service_client_id=ids.sid + "-worker",
            )
        )
        db.flush()
        db.add(Resource(id=ids.rid, workspace_id=ids.wid, resource_id=ids.sid))
        db.add(Context(id=ids.cid, workspace_id=ids.wid, name=ids.sid, resource_id=ids.sid))
        db.flush()
        db.add(
            SyncInstallation(
                id=ids.iid,
                workspace_id=ids.wid,
                service_id=ids.sid,
                service_client_id=ids.sid + "-worker",
                authorization_id=uuid4(),
                application_id="migration-test",
                operations=["records:write"],
                resource_pk=ids.rid,
                resource_id=ids.sid,
                context_id=ids.cid,
                endpoint_id=uuid4(),
            )
        )
        event = ResourceEvent(
            resource_pk=ids.rid,
            resource_id=ids.sid,
            op="upsert",
            doc_id="preserved",
            version=1,
            payload={"title": "Kept"},
        )
        db.add(event)
        db.flush()
        ids.event_id = event.id
    return ids


def _seed_sync(engine, ids):
    with Session(engine) as db, db.begin():
        db.add(SyncFeed(id=ids.fid, installation_id=ids.iid, kind="product", config_hash="1" * 64))
        db.flush()
        # The table, not the entity: the current model can map columns (f55's
        # change_count) that the revision under test does not have yet.
        for rid, state in ((ids.pending, "uploading"), (ids.committed, "committed")):
            db.execute(
                insert(SyncRun.__table__).values(
                    id=rid,
                    feed_id=ids.fid,
                    operation_id=uuid4(),
                    request_hash="2" * 64,
                    manifest={"retained": state},
                    generations=[1, 1, 1],
                    base_revision=0,
                    state=state,
                    expires_at=ids.now + timedelta(hours=24),
                )
            )
        db.flush()
        db.add(
            SyncChunk(
                run_id=ids.pending,
                chunk_index=0,
                digest="3" * 64,
                byte_count=100,
                record_count=1,
                records=[{"external_id": "pending"}],
            )
        )
        db.add(
            SyncRecord(
                feed_id=ids.fid,
                resource_pk=ids.rid,
                external_id="one",
                doc_id="preserved",
                version=1,
                payload={"title": "Kept"},
                last_event_id=ids.event_id,
            )
        )
        db.add(
            SyncIndexOutbox(
                run_id=ids.committed,
                resource_pk=ids.rid,
                context_id=ids.cid,
                last_event_id=ids.event_id,
                event_ids=[ids.event_id],
            )
        )


def _seed_budget(engine, ids):
    with Session(engine) as db, db.begin():
        db.add(
            PublicUsageBudget(
                workspace_id=ids.wid,
                period_started_at=ids.now - timedelta(days=2),
                calls_used=27123,
                period_seq=9,
                updated_at=ids.now,
            )
        )


def _assert_preserved(engine, ids, *, sync, budget):
    with Session(engine) as db:
        assert (
            db.scalar(select(User).where(User.user_id == ids.uid)).email
            == ids.uid + "@example.test"
        )
        assert db.get(Workspace, ids.wid).owner_user_id == ids.uid
        assert db.get(SyncInstallation, ids.iid).resource_pk == ids.rid
        assert db.get(ResourceEvent, ids.event_id).payload == {"title": "Kept"}
        if sync:
            assert db.get(SyncFeed, ids.fid).config_hash == "1" * 64
            assert db.scalar(select(SyncRun.manifest).where(SyncRun.id == ids.pending)) == {
                "retained": "uploading"
            }
            assert db.get(SyncChunk, (ids.pending, 0)).records == [{"external_id": "pending"}]
            assert db.scalar(select(SyncRecord).where(SyncRecord.feed_id == ids.fid)).payload == {
                "title": "Kept"
            }
            assert db.get(SyncIndexOutbox, ids.committed).event_ids == [ids.event_id]
        if budget:
            row = db.get(PublicUsageBudget, ids.wid)
            assert (row.period_started_at, row.calls_used, row.period_seq, row.updated_at) == (
                ids.now - timedelta(days=2),
                27123,
                9,
                ids.now,
            )


@pytest.mark.parametrize("starting_heads", [(COMMON,), (BUDGET,), (CLEAR,), (SYNC,), (SYNC, CLEAR)])
def test_upgrade_and_unmerge_preserve_both_branches(async_engine, starting_heads):
    engine = _sync_engine()
    try:
        current_head = ScriptDirectory.from_config(_get_alembic_config()).get_current_head()
        assert current_head is not None
        _reset_alembic_state()
        for revision in starting_heads:
            _migrate(command.upgrade, revision)
        ids = _seed_common(engine)
        tables = set(inspect(engine).get_table_names())
        has_sync, has_budget = "sync_feeds" in tables, "public_usage_budgets" in tables
        if has_sync:
            _seed_sync(engine, ids)
        if has_budget:
            _seed_budget(engine, ids)
        _migrate(command.upgrade, "head")
        with engine.connect() as conn:
            assert set(conn.scalars(text("SELECT version_num FROM alembic_version"))) == {
                current_head
            }
        _assert_preserved(engine, ids, sync=has_sync, budget=has_budget)
        if not has_sync:
            _seed_sync(engine, ids)
        if not has_budget:
            _seed_budget(engine, ids)
        for parent in (SYNC, CLEAR):
            # Revert any future descendants before testing this fixed merge node.
            _migrate(command.downgrade, MERGE)
            _migrate(command.downgrade, parent)
            with engine.connect() as conn:
                assert set(conn.scalars(text("SELECT version_num FROM alembic_version"))) == {
                    SYNC,
                    CLEAR,
                }
            _assert_preserved(engine, ids, sync=True, budget=True)
            _migrate(command.upgrade, "head")
            _assert_preserved(engine, ids, sync=True, budget=True)
    finally:
        try:
            _reset_alembic_state()
            _migrate(command.upgrade, "head")
        finally:
            engine.dispose()
