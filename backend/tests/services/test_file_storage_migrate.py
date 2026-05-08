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


class _FakeBlobStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}

    async def write_object(self, key, data, content_type, sha256):
        self.objects[key] = (data, content_type, sha256)

    async def head_object(self, key):
        if key not in self.objects:
            return None
        return {"size_bytes": len(self.objects[key][0]), "etag": self.objects[key][2]}

    async def delete_object(self, key):
        self.objects.pop(key, None)

    async def generate_presigned_put(self, key, content_type, size_bytes, ttl_seconds, sha256):
        return f"put://{key}"

    async def generate_presigned_get(self, key, filename, ttl_seconds):
        return f"get://{key}"


@pytest.fixture
def workspace_id():
    return uuid4()


@pytest.fixture
def attachment_id():
    return uuid4()


@pytest.fixture
def fake_storage():
    return _FakeBlobStorage()


@pytest.fixture
def db():
    db = MagicMock()
    db.execute = AsyncMock()
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
    async def test_happy_path_writes_r2_and_inserts_file_object(
        self, service, db, fake_storage, workspace_id, attachment_id
    ):
        _no_existing(db)
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
        happens. Re-running the script is safe."""
        existing = MagicMock()
        existing.id = uuid4()
        existing.storage_key = f"{workspace_id}/legacy/{attachment_id}/x.bin"

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
            )

        assert file is existing
        # No R2 write, no DB insert, no quota touch
        assert fake_storage.objects == {}
        db.add.assert_not_called()
        db.flush.assert_not_awaited()
        db.commit.assert_not_awaited()
        upsert.assert_not_awaited()
