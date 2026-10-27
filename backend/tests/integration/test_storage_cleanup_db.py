"""#2395: real PostgreSQL cascade, rollback, shared-key and writer races.

Set MINIO_TEST_ENDPOINT=http://127.0.0.1:19000 to run the same deletion tests
against an isolated MinIO bucket as well as the hermetic storage fake.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from models.auth import User, Workspace, WorkspaceMember
from models.file_objects import FileObject, StorageObjectDeletion
from services.context_service import ContextService
from services.storage_cleanup import (
    drain_storage_deletions,
    lock_storage_key,
    purge_deleted_workspace_files,
    sweep_unregistered_objects,
)
from services.workspace_service import WorkspaceService
from storage.s3_compatible import S3CompatibleStorage
from tasks import file_tasks
from tests.storage._fakes import FakeBlobStorage
from utils.datetime import utcnow

from ._admin_helpers import make_context, make_user, make_workspace


@pytest_asyncio.fixture(scope="module", loop_scope="session", autouse=True)
async def cleanup_triggers(async_engine):
    path = Path(__file__).parents[2] / "alembic/versions/f18_2395_file_cleanup.py"
    spec = importlib.util.spec_from_file_location("file_cleanup_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with async_engine.begin() as conn:
        await conn.execute(text("DROP FUNCTION IF EXISTS lock_file_object_keys() CASCADE"))
        await conn.execute(text("DROP FUNCTION IF EXISTS enqueue_deleted_file_objects() CASCADE"))
        for statement in migration.DDL:
            await conn.execute(text(statement))
    yield
    async with async_engine.begin() as conn:
        for statement in (
            "DROP TRIGGER file_object_delete_cleanup ON file_objects",
            "DROP TRIGGER file_object_update_keys ON file_objects",
            "DROP TRIGGER file_object_insert_keys ON file_objects",
            "DROP FUNCTION enqueue_deleted_file_objects()",
            "DROP FUNCTION lock_file_object_keys()",
        ):
            await conn.execute(text(statement))


@pytest_asyncio.fixture(params=["fake", "minio"])
async def storage(request):
    if request.param == "fake":
        yield FakeBlobStorage()
        return
    endpoint = os.getenv("MINIO_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("set MINIO_TEST_ENDPOINT for real MinIO acceptance tests")
    bucket = "kmc-2395-" + uuid4().hex
    store = S3CompatibleStorage(
        account_id="test",
        access_key_id="storage-test-access",
        secret_access_key="storage-test-secret",
        bucket=bucket,
        endpoint_url=endpoint,
    )
    async with store._client() as client:
        await client.create_bucket(Bucket=bucket)
    yield store
    async with store._client() as client:
        listing = await client.list_objects_v2(Bucket=bucket)
        for obj in listing.get("Contents", []):
            await client.delete_object(Bucket=bucket, Key=obj["Key"])
        await client.delete_bucket(Bucket=bucket)


@pytest_asyncio.fixture
async def world(db_session):
    user = make_user()
    ws = make_workspace(owner_user_id=user.user_id)
    ctx = make_context(workspace_id=ws.id, created_by=user.user_id)
    db_session.add_all([user, ws])
    await db_session.flush()
    db_session.add_all(
        [ctx, WorkspaceMember(workspace_id=ws.id, user_id=user.user_id, role="admin")]
    )
    await db_session.commit()
    ids = (user.user_id, ws.id, ctx.id)
    yield ids
    await db_session.rollback()
    await db_session.execute(delete(Workspace).where(Workspace.id == ids[1]))
    await db_session.execute(delete(User).where(User.user_id == ids[0]))
    await db_session.execute(
        delete(StorageObjectDeletion).where(
            StorageObjectDeletion.storage_key.startswith(str(ids[1]) + "/")
        )
    )
    await db_session.commit()


async def add_file(db, storage, world, *, status="uploaded", expires=None, context_bound=True):
    owner, ws_id, ctx_id = world
    sha = uuid4().hex + uuid4().hex
    key = f"{ws_id}/{sha[:2]}/{sha}"
    row = FileObject(
        id=uuid4(),
        workspace_id=ws_id,
        context_id=ctx_id if context_bound else None,
        sha256=sha,
        size_bytes=3,
        content_type="text/plain",
        filename="fixture.txt",
        storage_key=key,
        status=status,
        created_by=owner,
        expires_at=expires,
    )
    db.add(row)
    await db.commit()
    await storage.write_object(key, b"abc", "text/plain", sha)
    return row.id, key, sha


@pytest.mark.asyncio
async def test_workspace_cascade_retains_retry_after_storage_failure(db_session, world, storage):
    _, key, _ = await add_file(db_session, storage, world)
    await db_session.execute(delete(Workspace).where(Workspace.id == world[1]))
    await db_session.commit()
    assert await db_session.scalar(select(StorageObjectDeletion.storage_key)) == key
    real_delete = storage.delete_object
    with patch.object(
        storage, "delete_object", AsyncMock(side_effect=RuntimeError("private key text"))
    ):
        counts = await drain_storage_deletions(db_session, storage)
    assert counts["failed"] == 1
    assert await db_session.scalar(select(StorageObjectDeletion.storage_key)) == key
    assert await storage.head_object(key) is not None
    with patch.object(storage, "delete_object", real_delete):
        assert (await drain_storage_deletions(db_session, storage))["deleted"] == 1
        assert (await drain_storage_deletions(db_session, storage))["deleted"] == 0
    assert await storage.head_object(key) is None


@pytest.mark.asyncio
async def test_rollback_cannot_enqueue_committed_cleanup(db_session, world, storage):
    _, key, _ = await add_file(db_session, storage, world)
    await db_session.execute(delete(Workspace).where(Workspace.id == world[1]))
    assert await db_session.scalar(select(StorageObjectDeletion.storage_key)) == key
    await db_session.rollback()
    assert await db_session.scalar(select(StorageObjectDeletion.storage_key)) is None
    assert await storage.head_object(key) is not None


@pytest.mark.asyncio
async def test_late_presigned_put_keeps_marker_until_expiry_plus_grace(db_session, world, storage):
    expires = utcnow() + timedelta(minutes=5)
    _, key, _ = await add_file(db_session, storage, world, status="reserved", expires=expires)
    await db_session.execute(delete(Workspace).where(Workspace.id == world[1]))
    await db_session.commit()
    job = await db_session.get(StorageObjectDeletion, key)
    assert job.available_after == expires + timedelta(hours=1)
    assert (await drain_storage_deletions(db_session, storage))["deleted"] == 0
    await storage.write_object(key, b"late", "text/plain", "a" * 64)
    await db_session.execute(update(StorageObjectDeletion).values(available_after=utcnow()))
    await db_session.commit()
    assert (await drain_storage_deletions(db_session, storage))["deleted"] == 1
    assert await storage.head_object(key) is None


@pytest.mark.asyncio
async def test_soft_workspace_and_context_delete_keep_registry_until_gc(db_session, world, storage):
    _, key, _ = await add_file(db_session, storage, world)
    with (
        patch("db.qdrant.delete_context_points_everywhere", AsyncMock(return_value={})),
        patch("services.storage_quota_service.release_storage_bytes", AsyncMock()),
        patch.object(ContextService, "_delete_context_collection", AsyncMock()),
        patch("db.qdrant.list_memory_collections", AsyncMock(return_value=[])),
    ):
        await ContextService(db_session).delete_context(world[0], world[2])
        assert await storage.head_object(key) is not None  # restore window preserved
        _, workspace_key, _ = await add_file(db_session, storage, world, context_bound=False)
        await WorkspaceService(db_session).delete_workspace(world[1], world[0])
        assert await storage.head_object(workspace_key) is not None
    await db_session.execute(
        update(FileObject)
        .where(FileObject.workspace_id == world[1])
        .values(deleted_at=utcnow() - timedelta(days=8))
    )
    await db_session.commit()

    async def sessions():
        yield db_session

    with (
        patch.object(file_tasks, "get_db", sessions),
        patch.object(file_tasks, "get_blob_storage", return_value=storage),
    ):
        counts = await file_tasks.sweep_soft_deleted_files()
    assert counts["swept"] == 2
    assert await storage.head_object(key) is None
    assert await storage.head_object(workspace_key) is None


@pytest.mark.asyncio
async def test_shared_key_reserved_and_soft_deleted_registry_protect_object(
    db_session, world, storage
):
    file_id, key, sha = await add_file(db_session, storage, world)
    await db_session.execute(
        update(FileObject).where(FileObject.id == file_id).values(deleted_at=utcnow())
    )
    await db_session.commit()
    other = FileObject(
        id=uuid4(),
        workspace_id=world[1],
        sha256=sha,
        size_bytes=3,
        content_type="text/plain",
        filename="live.txt",
        storage_key=key,
        status="reserved",
        created_by=world[0],
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.execute(delete(FileObject).where(FileObject.id == file_id))
    await db_session.commit()
    assert (await drain_storage_deletions(db_session, storage))["registered"] == 1
    assert await storage.head_object(key) is not None
    await db_session.execute(
        update(FileObject)
        .where(FileObject.id == other.id)
        .values(deleted_at=utcnow(), status="failed")
    )
    await db_session.commit()
    db_session.add(StorageObjectDeletion(storage_key=key, available_after=utcnow()))
    await db_session.commit()
    assert (await drain_storage_deletions(db_session, storage))["registered"] == 1
    assert await storage.head_object(key) is not None


@pytest.mark.asyncio
async def test_new_upload_waits_for_cleanup_key_lock(async_engine, db_session, world, storage):
    _, key, sha = await add_file(db_session, storage, world)
    await db_session.execute(delete(FileObject).where(FileObject.storage_key == key))
    await db_session.commit()
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    started = asyncio.Event()
    flushed = asyncio.Event()
    writer_pid = []

    async def upload():
        async with sessions() as writer:
            writer_pid.append(await writer.scalar(text("SELECT pg_backend_pid()")))
            writer.add(
                FileObject(
                    id=uuid4(),
                    workspace_id=world[1],
                    sha256=sha,
                    size_bytes=3,
                    content_type="text/plain",
                    filename="new.txt",
                    storage_key=key,
                    status="reserved",
                    created_by=world[0],
                )
            )
            started.set()
            await writer.flush()  # INSERT transition trigger waits for cleanup
            flushed.set()
            await writer.commit()
            await storage.write_object(key, b"new", "text/plain", sha)

    await lock_storage_key(db_session, key)
    task = asyncio.create_task(upload())
    await started.wait()
    async with asyncio.timeout(5):
        while not await db_session.scalar(
            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": writer_pid[0]}
        ):
            await asyncio.sleep(0.01)
    assert not flushed.is_set()
    assert (await drain_storage_deletions(db_session, storage))["deleted"] == 1
    await asyncio.wait_for(task, timeout=5)
    assert await storage.head_object(key) is not None


class OldInventory(FakeBlobStorage):
    async def iter_objects(self):
        for key in list(self.objects):
            yield {"key": key, "last_modified": utcnow() - timedelta(days=3)}

    async def head_object(self, key):
        meta = await super().head_object(key)
        if meta:
            meta["last_modified"] = utcnow() - timedelta(days=3)
        return meta


@pytest.mark.asyncio
async def test_orphan_inventory_plan_apply_and_live_registry_recheck(db_session, world):
    storage = OldInventory()
    _, live, _ = await add_file(db_session, storage, world)
    orphan = f"{world[1]}/legacy/{uuid4()}/private-name.txt"
    await storage.write_object(orphan, b"old", "text/plain", "b" * 64)
    await storage.write_object("other-application/private.txt", b"keep", "text/plain", "c" * 64)
    plan = await sweep_unregistered_objects(db_session, storage)
    assert plan == {"scanned": 3, "candidates": 1, "deleted": 0, "failed": 0, "kept": 2}
    assert await storage.head_object(orphan) is not None
    applied = await sweep_unregistered_objects(db_session, storage, dry_run=False)
    assert applied["deleted"] == 1
    assert await storage.head_object(orphan) is None
    assert await storage.head_object(live) is not None


@pytest.mark.asyncio
async def test_gc_and_orphan_sweep_keep_younger_soft_deleted_holder(db_session, world, storage):
    old_id, key, sha = await add_file(db_session, storage, world)
    await db_session.execute(
        update(FileObject)
        .where(FileObject.id == old_id)
        .values(deleted_at=utcnow() - timedelta(days=8))
    )
    await db_session.commit()
    younger = FileObject(
        id=uuid4(),
        workspace_id=world[1],
        sha256=sha,
        size_bytes=3,
        content_type="text/plain",
        filename="younger.txt",
        storage_key=key,
        status="uploaded",
        created_by=world[0],
        deleted_at=utcnow() - timedelta(days=1),
    )
    db_session.add(younger)
    await db_session.commit()

    async def sessions():
        yield db_session

    with (
        patch.object(file_tasks, "get_db", sessions),
        patch.object(file_tasks, "get_blob_storage", return_value=storage),
    ):
        counts = await file_tasks.sweep_soft_deleted_files()
    assert counts["swept"] == 1 and counts["object_kept_shared"] == 1
    assert await storage.head_object(key) is not None
    assert await db_session.get(FileObject, younger.id) is not None

    reservation = FileObject(
        id=uuid4(),
        workspace_id=world[1],
        sha256=sha,
        size_bytes=3,
        content_type="text/plain",
        filename="expired.txt",
        storage_key=key,
        status="reserved",
        created_by=world[0],
        expires_at=utcnow() - timedelta(hours=2),
    )
    db_session.add(reservation)
    await db_session.commit()
    with (
        patch.object(file_tasks, "get_db", sessions),
        patch.object(file_tasks, "get_blob_storage", return_value=storage),
        patch("services.storage_quota_service.release_storage_bytes", AsyncMock()),
    ):
        counts = await file_tasks.sweep_orphan_files()
    assert counts["swept"] == 1 and counts["r2_deleted"] == 0
    assert await storage.head_object(key) is not None


@pytest.mark.asyncio
async def test_old_workspace_backfill_is_bounded_preserves_timestamp_and_rechecks_state(
    db_session, world, storage
):
    _, key, _ = await add_file(db_session, storage, world)
    _, second_key, _ = await add_file(db_session, storage, world, context_bound=False)
    deleted = utcnow() - timedelta(days=8)
    await db_session.execute(
        update(Workspace).where(Workspace.id == world[1]).values(deleted_at=deleted)
    )
    await db_session.commit()
    with patch("services.storage_quota_service.release_storage_bytes", AsyncMock()):
        assert (await purge_deleted_workspace_files(db_session, file_limit=1))[
            "backfilled_files"
        ] == 1
        assert (await purge_deleted_workspace_files(db_session, file_limit=1))[
            "backfilled_files"
        ] == 1
        assert (await purge_deleted_workspace_files(db_session, file_limit=1))[
            "backfilled_files"
        ] == 0
    rows = list(
        (
            await db_session.scalars(select(FileObject).where(FileObject.workspace_id == world[1]))
        ).all()
    )
    assert all(row.deleted_at == deleted for row in rows)
    assert await storage.head_object(key) is not None
    assert await storage.head_object(second_key) is not None
    # A workspace revived before the sweep is neither selected nor purged.
    await db_session.execute(
        update(Workspace).where(Workspace.id == world[1]).values(deleted_at=None)
    )
    await db_session.execute(
        update(FileObject).where(FileObject.workspace_id == world[1]).values(deleted_at=None)
    )
    await db_session.commit()
    assert (await purge_deleted_workspace_files(db_session))["backfilled_files"] == 0
