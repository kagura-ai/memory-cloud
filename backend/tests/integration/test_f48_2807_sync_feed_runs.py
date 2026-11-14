"""Real upgrade/downgrade preserves pre-existing targets and committed events."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from models.auth import Context, User, Workspace
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

PARENT = "f47_2819_merge_sync_credentials"
REVISION = "f48_2807_sync_feed_runs"
TABLES = {"sync_feeds", "sync_runs", "sync_chunks", "sync_records", "sync_index_outbox"}


def test_feed_migration_roundtrip_constraints_cascades_and_event_preservation(async_engine):
    engine = _sync_engine()
    uid, sid = "migration-" + uuid4().hex, "service-" + uuid4().hex
    wid, rid, cid, iid, fid, run_id = (uuid4() for _ in range(6))
    now = utcnow()
    slug = "sync-" + rid.hex
    try:
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PARENT)
        with Session(engine) as db, db.begin():
            db.add(User(user_id=uid, email=uid + "@example.test"))
            db.flush()
            db.add(Workspace(id=wid, name="Preserved", owner_user_id=uid))
            db.add(
                SyncService(
                    service_id=sid,
                    browser_client_id=sid + "-browser",
                    service_client_id=sid + "-worker",
                )
            )
            db.flush()
            db.add(Resource(id=rid, workspace_id=wid, resource_id=slug))
            db.add(
                Context(
                    id=cid,
                    workspace_id=wid,
                    name=slug,
                    resource_id=slug,
                    is_public=True,
                    is_private=False,
                )
            )
            db.flush()
            db.add(
                SyncInstallation(
                    id=iid,
                    workspace_id=wid,
                    service_id=sid,
                    service_client_id=sid + "-worker",
                    authorization_id=uuid4(),
                    application_id="migration",
                    operations=["records:write"],
                    resource_pk=rid,
                    context_id=cid,
                    endpoint_id=uuid4(),
                    resource_id=slug,
                    state="active",
                    generation=1,
                )
            )
        for _cycle in range(2):
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), REVISION)
            assert TABLES.issubset(inspect(engine).get_table_names())
            with Session(engine) as db, db.begin():
                db.add(SyncFeed(id=fid, installation_id=iid, kind="product", config_hash="1" * 64))
                db.flush()
                db.add(
                    SyncRun(
                        id=run_id,
                        feed_id=fid,
                        operation_id=uuid4(),
                        request_hash="2" * 64,
                        manifest={},
                        generations=[1, 1, 1],
                        base_revision=0,
                        state="uploading",
                        expires_at=now + timedelta(hours=24),
                    )
                )
                db.flush()
                for changes in (
                    {"chunk_index": -1},
                    {"byte_count": 524289},
                    {"digest": "short"},
                    {"record_count": 101},
                ):
                    with pytest.raises(IntegrityError), db.begin_nested():
                        db.add(
                            SyncChunk(
                                **(
                                    {
                                        "run_id": run_id,
                                        "chunk_index": 0,
                                        "digest": "3" * 64,
                                        "byte_count": 100,
                                        "record_count": 1,
                                        "records": [],
                                    }
                                    | changes
                                )
                            )
                        )
                        db.flush()
                record = ResourceEvent(
                    resource_pk=rid,
                    resource_id=slug,
                    op="upsert",
                    doc_id="retained-" + str(_cycle),
                    version=1,
                    payload={"title": "Preserved data"},
                )
                db.add(record)
                db.flush()
                db.add(
                    SyncRecord(
                        feed_id=fid,
                        resource_pk=rid,
                        external_id="one",
                        doc_id="retained-" + str(_cycle),
                        version=1,
                        payload={"title": "Preserved data"},
                        last_event_id=record.id,
                    )
                )
                db.add(
                    SyncChunk(
                        run_id=run_id,
                        chunk_index=0,
                        digest="3" * 64,
                        byte_count=100,
                        record_count=1,
                        records=[],
                    )
                )
                db.add(
                    SyncIndexOutbox(
                        run_id=run_id,
                        resource_pk=rid,
                        context_id=cid,
                        last_event_id=record.id,
                        event_ids=[record.id],
                    )
                )
            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PARENT)
            assert TABLES.isdisjoint(inspect(engine).get_table_names())
            with Session(engine) as db:
                assert db.get(Resource, rid) is not None
                assert db.get(SyncInstallation, iid) is not None
                assert (
                    db.scalar(
                        select(ResourceEvent).where(
                            ResourceEvent.doc_id == "retained-" + str(_cycle)
                        )
                    )
                    is not None
                )
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), REVISION)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
    finally:
        try:
            # Other modules may already hold the session-scoped async_engine.
            # Leave its database at head, rather than dropping its live schema.
            _reset_alembic_state()
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")
        finally:
            engine.dispose()
