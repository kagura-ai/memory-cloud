"""Tests for ``FileStorageService.migrate_attachment`` (Issue #485 Commit 9).

The migration logic copies legacy ``Attachment`` BYTEA blobs to R2 and
inserts a corresponding ``file_objects`` row with ``status='uploaded'``.
Idempotent: re-running on already-migrated rows is a no-op.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.file_storage_service import FileStorageService
from tests.storage._fakes import FakeBlobStorage


@pytest.fixture
def workspace_id():
    return uuid4()


@pytest.fixture
def attachment_id():
    return uuid4()


@pytest.fixture
def fake_storage():
    return FakeBlobStorage()


@pytest.fixture
def db(workspace_id):
    db = MagicMock()
    db.execute = AsyncMock()
    db.scalar = AsyncMock(return_value=workspace_id)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def service(db, fake_storage):
    return FileStorageService(db, storage=fake_storage)


def _no_existing(db):
    """Patch the existing-row lookup to return None (no prior migration)."""
    miss = MagicMock()
    miss.scalar_one_or_none = MagicMock(return_value=None)
    db.execute.return_value = miss


class TestMigrateAttachment:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bound", [True, False], ids=["context", "workspace-shared"])
    async def test_happy_path_writes_r2_and_inserts_file_object(
        self, service, db, fake_storage, workspace_id, attachment_id, bound
    ):
        _no_existing(db)
        # #2647: the row carries the memory's context; NULL only for a
        # context-less memory (workspace-shared).
        context_id = uuid4() if bound else None
        data = b"legacy-bytea-bytes"
        expected_sha = hashlib.sha256(data).hexdigest()

        with patch.object(FileStorageService, "_upsert_workspace_usage", AsyncMock()) as upsert:
            file = await service.migrate_attachment(
                workspace_id=workspace_id,
                attachment_id=attachment_id,
                filename="report.pdf",
                content_type="application/pdf",
                size_bytes=len(data),
                data=data,
                created_by="migration:test",
                context_id=context_id,
            )

        # R2 write happened with correct sha256
        expected_key = f"{workspace_id}/legacy/{attachment_id}/report.pdf"
        assert expected_key in fake_storage.objects
        body, content_type, stored_sha = fake_storage.objects[expected_key]
        assert body == data
        assert content_type == "application/pdf"
        assert stored_sha == expected_sha

        # FileObject row inserted with status=uploaded
        assert file.status == "uploaded"
        assert file.uploaded_at is not None
        assert file.sha256 == expected_sha
        assert file.storage_backend == "r2"
        assert file.storage_key == expected_key
        assert file.context_id == context_id
        db.add.assert_called_once_with(file)
        # The workspace row is locked, and the context row too when there is one.
        assert db.scalar.await_count == (2 if bound else 1)

        # workspace_storage_usage incremented
        upsert.assert_awaited_once()
        kwargs = upsert.call_args.kwargs
        assert kwargs["delta_bytes"] == len(data)
        assert kwargs["delta_files"] == 1

        # Atomic commit
        db.flush.assert_awaited_once()
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotent_skip_when_already_migrated(
        self, service, db, fake_storage, workspace_id, attachment_id
    ):
        """If a file_objects row with the same legacy key exists, the
        migration is a no-op — bytes are not re-uploaded and no INSERT
        happens. Re-running the script is safe. The existing row is returned
        as is even when a context is passed: re-binding an old row is the
        script's ``--repair-context`` job (#2647), not this method's."""
        existing = MagicMock()
        existing.id = uuid4()
        existing.storage_key = f"{workspace_id}/legacy/{attachment_id}/x.bin"
        existing.context_id = None
        context_id = uuid4()

        hit = MagicMock()
        hit.scalar_one_or_none = MagicMock(return_value=existing)
        db.execute.return_value = hit

        with patch.object(FileStorageService, "_upsert_workspace_usage", AsyncMock()) as upsert:
            file = await service.migrate_attachment(
                workspace_id=workspace_id,
                attachment_id=attachment_id,
                filename="x.bin",
                content_type="application/octet-stream",
                size_bytes=100,
                data=b"x" * 100,
                created_by="migration:test",
                context_id=context_id,
            )

        assert file is existing
        assert existing.context_id is None  # untouched
        # No R2 write, no DB insert, no quota touch
        assert fake_storage.objects == {}
        db.add.assert_not_called()
        db.flush.assert_not_awaited()
        db.commit.assert_not_awaited()
        upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_workspace_is_rejected_before_writing_bytes(
    service, db, fake_storage, workspace_id, attachment_id
):
    from utils.exceptions import NotFoundException

    db.scalar.return_value = None
    with pytest.raises(NotFoundException):
        await service.migrate_attachment(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            filename="x.bin",
            content_type="application/octet-stream",
            size_bytes=3,
            data=b"abc",
            created_by="migration:test",
            context_id=None,
        )
    assert fake_storage.objects == {}
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_deleted_context_is_rejected_before_writing_bytes(
    service, db, fake_storage, workspace_id, attachment_id
):
    """#2647: the context row is locked (KEY SHARE, live, same workspace) right
    after the workspace row, before the storage-key lock and the R2 write. A
    context deleted after the script read its page is refused there, so no
    bytes land without a registry row and no live row lands in a deleted
    context."""
    from sqlalchemy.dialects import postgresql

    from utils.exceptions import NotFoundException

    context_id = uuid4()
    db.scalar.side_effect = [workspace_id, None]  # workspace live, context gone
    with pytest.raises(NotFoundException, match="context not found"):
        await service.migrate_attachment(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            filename="x.bin",
            content_type="application/octet-stream",
            size_bytes=3,
            data=b"abc",
            created_by="migration:test",
            context_id=context_id,
        )
    assert fake_storage.objects == {}
    db.add.assert_not_called()
    db.execute.assert_not_awaited()  # neither the storage-key lock nor the lookup ran

    context_lock = db.scalar.await_args_list[1].args[0]
    sql = str(context_lock.compile(dialect=postgresql.dialect()))
    assert "FROM contexts" in sql
    assert "contexts.deleted_at IS NULL" in sql
    assert "contexts.workspace_id" in sql
    assert sql.rstrip().endswith("FOR KEY SHARE")
