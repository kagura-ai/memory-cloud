"""Unit tests for ``FileStorageService`` (Issue #485).

Mocks the DB session and uses the in-memory ``BlobStorageProtocol``
fake from ``tests.storage.test_protocol`` so this file does not depend
on aioboto3 or a real Redis. SQL constraint behaviour (partial unique
violations, CHECK constraints, computed columns) is covered by the
integration suite gated on ``make test-integration``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from services.file_storage_service import FileStorageService, ReserveResult
from utils.exceptions import (
    ConflictError,
    NotFoundException,
    QuotaExceededError,
    ValidationError,
)


# Reusable in-memory blob storage matching BlobStorageProtocol
class _FakeBlobStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.head_size_override: int | None = None

    async def write_object(self, key, data, content_type, sha256):
        self.objects[key] = (data, content_type, sha256)

    async def head_object(self, key):
        if key not in self.objects:
            return None
        size = self.head_size_override
        if size is None:
            size = len(self.objects[key][0])
        return {"size_bytes": size, "etag": self.objects[key][2]}

    async def delete_object(self, key):
        self.objects.pop(key, None)

    async def generate_presigned_put(self, key, content_type, size_bytes, ttl_seconds):
        return f"https://test.local/put/{key}?size={size_bytes}&ttl={ttl_seconds}"

    async def generate_presigned_get(self, key, filename, ttl_seconds):
        return f"https://test.local/get/{key}?file={filename}&ttl={ttl_seconds}"


VALID_SHA = "a" * 64


def _make_workspace(workspace_id: UUID, *, storage_limit_bytes: int = 100 * 1024 * 1024):
    """Mock Workspace exposing only the attributes FileStorageService reads."""
    ws = MagicMock()
    ws.id = workspace_id
    ws.effective_storage_limit_bytes = storage_limit_bytes
    return ws


def _make_file_object(
    workspace_id: UUID,
    *,
    file_id: UUID | None = None,
    sha256: str = VALID_SHA,
    size_bytes: int = 1024,
    status: str = "reserved",
    deleted_at: datetime | None = None,
):
    file = MagicMock()
    file.id = file_id or uuid4()
    file.workspace_id = workspace_id
    file.sha256 = sha256
    file.size_bytes = size_bytes
    file.content_type = "application/octet-stream"
    file.filename = "test.bin"
    file.storage_backend = "r2"
    file.storage_key = f"{workspace_id}/{sha256[:2]}/{sha256}"
    file.status = status
    file.deleted_at = deleted_at
    file.expires_at = None
    file.uploaded_at = None
    return file


@pytest.fixture
def workspace_id():
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


def _patch_quota_reserve(succeed: bool = True):
    if succeed:
        return patch(
            "services.file_storage_service.storage_quota_service.reserve_storage_bytes",
            AsyncMock(),
        )
    return patch(
        "services.file_storage_service.storage_quota_service.reserve_storage_bytes",
        AsyncMock(side_effect=QuotaExceededError("quota exceeded")),
    )


def _patch_quota_release():
    return patch(
        "services.file_storage_service.storage_quota_service.release_storage_bytes",
        AsyncMock(),
    )


class TestReserveUpload:
    @pytest.mark.asyncio
    async def test_happy_path(self, service, db, workspace_id):
        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=True):
            out = await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="user-1",
                filename="test.bin",
                content_type="application/octet-stream",
                size_bytes=1024,
                sha256=VALID_SHA,
            )

        assert isinstance(out, ReserveResult)
        assert out.upload_url.startswith("https://test.local/put/")
        db.add.assert_called_once()
        db.flush.assert_awaited_once()
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_zero_size_raises_validation(self, service, workspace_id):
        with pytest.raises(ValidationError, match="size_bytes"):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x",
                content_type="application/octet-stream",
                size_bytes=0,
                sha256=VALID_SHA,
            )

    @pytest.mark.asyncio
    async def test_oversize_raises_validation(self, service, workspace_id):
        too_big = 200 * 1024 * 1024  # > 100 MiB Phase 1 cap
        with pytest.raises(ValidationError, match="size_bytes"):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x",
                content_type="application/octet-stream",
                size_bytes=too_big,
                sha256=VALID_SHA,
            )

    @pytest.mark.asyncio
    async def test_invalid_sha256_raises_validation(self, service, workspace_id):
        with pytest.raises(ValidationError, match="sha256"):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x",
                content_type="application/octet-stream",
                size_bytes=1024,
                sha256="too-short",
            )

    @pytest.mark.asyncio
    async def test_quota_exceeded_propagates(self, service, db, workspace_id):
        ws = _make_workspace(workspace_id, storage_limit_bytes=100)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=False):
            with pytest.raises(QuotaExceededError):
                await service.reserve_upload(
                    workspace_id=workspace_id,
                    created_by="u",
                    filename="x",
                    content_type="application/octet-stream",
                    size_bytes=1024,
                    sha256=VALID_SHA,
                )
        # No DB flush should have happened.
        db.flush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_workspace_not_found_raises(self, service, db, workspace_id):
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute.return_value = result

        with pytest.raises(NotFoundException, match="workspace"):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x",
                content_type="application/octet-stream",
                size_bytes=1024,
                sha256=VALID_SHA,
            )

    @pytest.mark.asyncio
    async def test_unique_constraint_violation_releases_reservation(
        self, service, db, workspace_id
    ):
        """If the DB flush hits the partial-unique index, the Redis
        reservation MUST be rolled back so the workspace doesn't lose
        capacity."""
        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result
        db.flush.side_effect = Exception(
            "duplicate key value violates unique constraint "
            '"uq_file_objects_workspace_sha256_active"'
        )

        with _patch_quota_reserve(succeed=True), _patch_quota_release() as release:
            with pytest.raises(ConflictError, match="already exists"):
                await service.reserve_upload(
                    workspace_id=workspace_id,
                    created_by="u",
                    filename="x",
                    content_type="application/octet-stream",
                    size_bytes=1024,
                    sha256=VALID_SHA,
                )
            release.assert_awaited_once()


class TestConfirmUpload:
    @pytest.mark.asyncio
    async def test_happy_path_transitions_to_uploaded(
        self, service, db, fake_storage, workspace_id
    ):
        file = _make_file_object(workspace_id, status="reserved")
        # Pre-populate the fake R2 object so head_object returns the right size
        await fake_storage.write_object(
            file.storage_key, b"x" * file.size_bytes, file.content_type, file.sha256
        )

        load_result = MagicMock()
        load_result.scalar_one_or_none = MagicMock(return_value=file)
        db.execute.return_value = load_result

        out = await service.confirm_upload(
            workspace_id=workspace_id, file_id=file.id, sha256=file.sha256
        )

        assert out.status == "uploaded"
        assert out.uploaded_at is not None
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotent_retry_returns_existing(self, service, db, workspace_id):
        """Already-uploaded row + matching sha256 → no-op return."""
        file = _make_file_object(workspace_id, status="uploaded")
        load_result = MagicMock()
        load_result.scalar_one_or_none = MagicMock(return_value=file)
        db.execute.return_value = load_result

        out = await service.confirm_upload(
            workspace_id=workspace_id, file_id=file.id, sha256=file.sha256
        )
        assert out is file
        db.commit.assert_not_awaited()  # purely a read

    @pytest.mark.asyncio
    async def test_sha256_mismatch_raises(self, service, db, workspace_id):
        file = _make_file_object(workspace_id, status="reserved", sha256=VALID_SHA)
        load_result = MagicMock()
        load_result.scalar_one_or_none = MagicMock(return_value=file)
        db.execute.return_value = load_result

        with pytest.raises(ValidationError, match="sha256 mismatch"):
            await service.confirm_upload(
                workspace_id=workspace_id, file_id=file.id, sha256="b" * 64
            )

    @pytest.mark.asyncio
    async def test_r2_object_missing_raises_conflict(self, service, db, workspace_id):
        file = _make_file_object(workspace_id, status="reserved")
        load_result = MagicMock()
        load_result.scalar_one_or_none = MagicMock(return_value=file)
        db.execute.return_value = load_result
        # fake_storage starts empty — head_object returns None

        with pytest.raises(ConflictError, match="R2 object missing"):
            await service.confirm_upload(
                workspace_id=workspace_id, file_id=file.id, sha256=file.sha256
            )

    @pytest.mark.asyncio
    async def test_truncated_upload_refunds_difference(
        self, service, db, fake_storage, workspace_id
    ):
        """If R2 reports a smaller size than reserved, the diff must be
        released back to the quota counter."""
        file = _make_file_object(workspace_id, status="reserved", size_bytes=2000)
        await fake_storage.write_object(
            file.storage_key, b"x" * 2000, file.content_type, file.sha256
        )
        fake_storage.head_size_override = 1500  # truncated by 500

        load_result = MagicMock()
        load_result.scalar_one_or_none = MagicMock(return_value=file)
        db.execute.return_value = load_result

        with _patch_quota_release() as release:
            out = await service.confirm_upload(
                workspace_id=workspace_id, file_id=file.id, sha256=file.sha256
            )

        assert out.size_bytes == 1500
        release.assert_awaited_once()
        kwargs = release.call_args.kwargs
        assert kwargs["size_bytes"] == 500


class TestDeleteFile:
    @pytest.mark.asyncio
    async def test_uploaded_file_releases_quota_immediately(self, service, db, workspace_id):
        """R5: soft-delete must call release_storage_bytes in the same txn."""
        file = _make_file_object(workspace_id, status="uploaded", size_bytes=1024)
        load_result = MagicMock()
        load_result.scalar_one_or_none = MagicMock(return_value=file)
        db.execute.return_value = load_result

        with _patch_quota_release() as release:
            await service.delete_file(workspace_id=workspace_id, file_id=file.id)

        assert file.deleted_at is not None
        release.assert_awaited_once()
        kwargs = release.call_args.kwargs
        assert kwargs["size_bytes"] == 1024
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reserved_file_skips_quota_release(self, service, db, workspace_id):
        """A still-reserved row has no committed quota — release would
        double-deduct via the orphan sweeper. So delete just marks it."""
        file = _make_file_object(workspace_id, status="reserved", size_bytes=1024)
        load_result = MagicMock()
        load_result.scalar_one_or_none = MagicMock(return_value=file)
        db.execute.return_value = load_result

        with _patch_quota_release() as release:
            await service.delete_file(workspace_id=workspace_id, file_id=file.id)

        assert file.deleted_at is not None
        release.assert_not_awaited()


class TestListFiles:
    @pytest.mark.asyncio
    async def test_invalid_limit_raises(self, service, workspace_id):
        with pytest.raises(ValidationError, match="limit"):
            await service.list_files(workspace_id=workspace_id, limit=0)
        with pytest.raises(ValidationError, match="limit"):
            await service.list_files(workspace_id=workspace_id, limit=10_000)

    @pytest.mark.asyncio
    async def test_returns_uploaded_rows(self, service, db, workspace_id):
        files = [
            _make_file_object(workspace_id, status="uploaded"),
            _make_file_object(workspace_id, status="uploaded"),
        ]
        list_result = MagicMock()
        list_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=files)))
        db.execute.return_value = list_result

        out = await service.list_files(workspace_id=workspace_id, limit=10)
        assert len(out) == 2
