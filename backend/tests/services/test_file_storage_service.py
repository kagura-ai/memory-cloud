"""Unit tests for ``FileStorageService`` (Issue #485).

Mocks the DB session and uses the shared in-memory ``BlobStorageProtocol``
fake from ``tests.storage._fakes`` so this file does not depend on
aioboto3 or a real Redis. SQL constraint behaviour (partial unique
violations, CHECK constraints, computed columns) is covered by the
integration suite gated on ``make test-integration``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from services.file_storage_service import FileStorageService, ReserveResult
from tests.storage._fakes import FakeBlobStorage
from utils.exceptions import (
    ConflictError,
    NotFoundException,
    QuotaExceededError,
    UnsupportedMediaTypeError,
    ValidationError,
)

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
    return FakeBlobStorage()


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
    @pytest.fixture(autouse=True)
    def _canonical_allowlist(self, monkeypatch):
        """Pin the allow-list to the canonical default for every test in
        this class so a developer with ``ALLOWED_FILE_CONTENT_TYPES``
        overridden in their shell env does not see spurious failures.

        Tests that need a different allow-list (empty fail-closed,
        parameter-laden entries) override this via their own
        ``monkeypatch.setattr`` — pytest stacks the undos in LIFO order
        so the inner override applies first and unwinds before this one.
        """
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "allowed_file_content_types",
            "image/png,image/jpeg,image/gif,application/pdf,"
            "text/plain,text/markdown,text/csv,application/json",
        )

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
                filename="report.pdf",
                content_type="application/pdf",
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
                content_type="application/pdf",
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
                content_type="application/pdf",
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
                content_type="application/pdf",
                size_bytes=1024,
                sha256="too-short",
            )

    @pytest.mark.asyncio
    async def test_uppercase_sha256_normalized_to_lowercase(self, service, db, workspace_id):
        """Postgres TEXT comparison is case-sensitive — without normalization,
        the same digest as 'AB...' and 'ab...' would land in separate rows
        under the partial unique index, defeating per-workspace dedup
        (Copilot review #574 finding). Normalization happens after regex
        validation so the db.add receives the lowercase form."""
        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=True):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                sha256="A" * 64,  # all uppercase — must be normalized
            )

        db.add.assert_called_once()
        added = db.add.call_args.args[0]
        assert added.sha256 == "a" * 64
        # storage_key is also derived from the lowercase form
        assert added.storage_key.endswith("a" * 64)

    @pytest.mark.parametrize(
        "bad_sha256",
        [
            "g" * 64,  # all non-hex
            "abcdef" * 10 + "    ",  # 64 chars but trailing whitespace
            "ab cd ef" * 8,  # 64 chars with internal whitespace — bytes.fromhex would silently
            #                 decode the 48 hex chars to 24 bytes, producing a malformed signed URL
            "Z" + "a" * 63,  # one bad char
        ],
        ids=["all-non-hex", "trailing-spaces", "internal-spaces", "leading-bad-char"],
    )
    @pytest.mark.asyncio
    async def test_non_hex_sha256_raises_validation(self, service, workspace_id, bad_sha256):
        """64-char strings with any non-hex chars must be rejected at the
        service boundary (#556). REST has a Pydantic regex; MCP coerces
        with bare ``str(...)`` so this is the safety net for both paths.
        Internal-whitespace input is the load-bearing case — ``bytes.fromhex``
        permissively decodes it (skipping spaces) and would silently produce
        a short base64 ChecksumSHA256 → 403 SignatureDoesNotMatch from R2
        when the flag is on."""
        with pytest.raises(ValidationError, match="non-hex"):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x",
                content_type="application/pdf",
                size_bytes=1024,
                sha256=bad_sha256,
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
                    content_type="application/pdf",
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
                content_type="application/pdf",
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
                    content_type="application/pdf",
                    size_bytes=1024,
                    sha256=VALID_SHA,
                )
            release.assert_awaited_once()

    # content_type allow-list — service-boundary check covers both REST + MCP.
    # Validation runs after size/sha256 and before workspace load, so rejection
    # short-circuits without DB or Redis I/O.

    @pytest.mark.asyncio
    async def test_allowed_content_type_passes(self, service, db, workspace_id):
        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=True):
            out = await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x.png",
                content_type="image/png",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        assert isinstance(out, ReserveResult)

    @pytest.mark.asyncio
    async def test_disallowed_content_type_raises_415(self, service, db, workspace_id):
        with pytest.raises(UnsupportedMediaTypeError) as exc_info:
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="malicious.exe",
                content_type="application/x-msdownload",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        err = exc_info.value
        assert err.status_code == 415
        assert err.error_code == "MEDIA-001"
        assert err.details["content_type"] == "application/x-msdownload"
        assert "image/png" in err.details["allowed"]
        # Rejection happens before workspace load — no DB execute.
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_case_insensitive_content_type(self, service, db, workspace_id):
        """RFC 6838: MIME type comparison is case-insensitive."""
        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=True):
            out = await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x.png",
                content_type="IMAGE/PNG",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        assert isinstance(out, ReserveResult)

    @pytest.mark.asyncio
    async def test_content_type_with_parameters_accepted(self, service, db, workspace_id):
        """RFC 7231: clients (browser fetch, multipart) commonly attach
        ``;charset=...`` parameters; the bare type/subtype should still match."""
        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=True):
            out = await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="notes.txt",
                content_type="text/plain; charset=utf-8",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        assert isinstance(out, ReserveResult)

    @pytest.mark.asyncio
    async def test_empty_allowlist_rejects_all(self, service, workspace_id, monkeypatch):
        """Empty/whitespace allow-list = fail-closed (every upload rejected)."""
        from config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "allowed_file_content_types", "")

        with pytest.raises(UnsupportedMediaTypeError):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                sha256=VALID_SHA,
            )

    @pytest.mark.parametrize(
        "bad_ct",
        [
            "image/png\r\nX-Injected: yes",  # CRLF header injection
            "image/png\nfoo",  # bare LF
            "image/png\rfoo",  # bare CR
            "image/png\x00null",  # NUL byte
        ],
    )
    @pytest.mark.asyncio
    async def test_control_chars_in_content_type_raise_validation(
        self, service, workspace_id, bad_ct
    ):
        """Defense in depth against R2 ContentType header injection — ValidationError (422), not 415."""
        with pytest.raises(ValidationError, match="control characters"):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x.png",
                content_type=bad_ct,
                size_bytes=1024,
                sha256=VALID_SHA,
            )

    @pytest.mark.parametrize(
        "bad_ct",
        [
            "no-slash",  # no separator
            "/no-type",  # empty type
            "no-subtype/",  # empty subtype
            ";",  # bare param separator
            "image/png garbage",  # space in subtype
            "",  # empty (Pydantic blocks this at REST, but MCP coerces str(...))
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_type_subtype_shape_raises_validation(
        self, service, workspace_id, bad_ct
    ):
        """Malformed type/subtype is a 422 shape error, not a 415 policy rejection."""
        with pytest.raises(ValidationError, match="type/subtype"):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x",
                content_type=bad_ct,
                size_bytes=1024,
                sha256=VALID_SHA,
            )

    @pytest.mark.asyncio
    async def test_oversize_filename_raises_validation(self, service, workspace_id):
        """MCP bypasses pydantic, so the service enforces the DB column cap."""
        with pytest.raises(ValidationError, match="filename"):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x" * 513,  # > 512
                content_type="application/pdf",
                size_bytes=1024,
                sha256=VALID_SHA,
            )

    @pytest.mark.asyncio
    async def test_oversize_content_type_raises_validation(self, service, workspace_id):
        """An MCP-injected oversize ``content_type`` that would otherwise
        normalize past the allow-list and fail at DB flush is rejected up
        front as a 422 ValidationError, not surfaced as a 500."""
        oversize = "application/pdf;" + "a" * 256  # 272 chars total
        with pytest.raises(ValidationError, match="content_type"):
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="x.pdf",
                content_type=oversize,
                size_bytes=1024,
                sha256=VALID_SHA,
            )

    def test_settings_rejects_malformed_allow_list_at_boot(self):
        """Boot-time fail-fast: malformed entries crash app startup so they
        never leak into ``UnsupportedMediaTypeError.details['allowed']``."""
        import pydantic

        from config.settings import Settings

        # Valid shapes are accepted (sanity).
        Settings(allowed_file_content_types="image/png,application/pdf")
        # Empty / whitespace / commas-only is fail-closed, NOT malformed.
        Settings(allowed_file_content_types="")
        Settings(allowed_file_content_types="   ")
        Settings(allowed_file_content_types=",,,")
        # Parameter-laden entries normalize and pass.
        Settings(allowed_file_content_types="text/plain; charset=utf-8, image/png")

        # Malformed shapes (no slash, embedded space, etc.) fail loud.
        with pytest.raises(pydantic.ValidationError, match="malformed entries"):
            Settings(allowed_file_content_types="image/png,garbage")
        with pytest.raises(pydantic.ValidationError, match="malformed entries"):
            Settings(allowed_file_content_types="image / png")
        with pytest.raises(pydantic.ValidationError, match="malformed entries"):
            Settings(allowed_file_content_types="/no-type")
        with pytest.raises(pydantic.ValidationError, match="malformed entries"):
            Settings(allowed_file_content_types="no-subtype/")

    @pytest.mark.asyncio
    async def test_env_allowlist_strips_parameters_in_entries(
        self, service, db, workspace_id, monkeypatch
    ):
        """An operator who pastes a parameter-laden MIME into the env var
        (``text/plain; charset=utf-8``) should still match a bare upload —
        without parser-side stripping the entry would silently never match."""
        from config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(
            settings,
            "allowed_file_content_types",
            "text/plain; charset=utf-8, application/pdf",
        )

        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=True):
            out = await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="notes.txt",
                content_type="text/plain",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        assert isinstance(out, ReserveResult)


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
        released back to the quota counter — but only AFTER the DB
        commit succeeds (Redis follows durable state)."""
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

    @pytest.mark.asyncio
    async def test_oversize_upload_rejects_as_conflict(
        self, service, db, fake_storage, workspace_id
    ):
        """Defence in depth: if R2 reports MORE bytes than declared,
        reject with ConflictError. Otherwise the per-file 100 MiB cap
        could be bypassed by PUTting a larger object than the
        reservation accepted."""
        file = _make_file_object(workspace_id, status="reserved", size_bytes=1000)
        await fake_storage.write_object(
            file.storage_key, b"x" * 1000, file.content_type, file.sha256
        )
        fake_storage.head_size_override = 5000  # reports more than declared

        load_result = MagicMock()
        load_result.scalar_one_or_none = MagicMock(return_value=file)
        db.execute.return_value = load_result

        with pytest.raises(ConflictError, match="malformed upload"):
            await service.confirm_upload(
                workspace_id=workspace_id, file_id=file.id, sha256=file.sha256
            )
        # The reservation is NOT released — operator should investigate
        # the orphan R2 object explicitly. The row stays ``reserved``
        # for the orphan sweeper to pick up.
        assert file.status == "reserved"


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
    async def test_reserved_file_releases_redis_reservation(self, service, db, workspace_id):
        """Cancelling a reserved upload MUST release the Redis reservation
        right away — otherwise the workspace stays over-counted until
        the orphan sweeper runs (15 min later by default), which on the
        Free 100 MiB tier blocks every subsequent upload (Copilot loop
        finding on PR #551)."""
        file = _make_file_object(workspace_id, status="reserved", size_bytes=1024)
        load_result = MagicMock()
        load_result.scalar_one_or_none = MagicMock(return_value=file)
        db.execute.return_value = load_result

        with _patch_quota_release() as release:
            await service.delete_file(workspace_id=workspace_id, file_id=file.id)

        assert file.deleted_at is not None
        release.assert_awaited_once()
        kwargs = release.call_args.kwargs
        assert kwargs["size_bytes"] == 1024

    @pytest.mark.asyncio
    async def test_failed_file_skips_quota_release(self, service, db, workspace_id):
        """``status='failed'`` rows already had their Redis quota released
        by the orphan sweeper — releasing again here would underflow."""
        file = _make_file_object(workspace_id, status="failed", size_bytes=1024)
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


class TestReserveUploadExtensionConsistency:
    """Issue #961: the declared content_type alone can launder a disallowed
    payload past the allow-list (an .svg declared text/plain → stored-XSS once
    served). ``reserve_upload`` now also derives the MIME implied by the
    filename extension and rejects (415) any inconsistency. The check lives in
    the shared service chokepoint, so both REST (POST /files/reserve) and MCP
    (handle_init_file_upload) inherit it.
    """

    @pytest.fixture(autouse=True)
    def _canonical_allowlist(self, monkeypatch):
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "allowed_file_content_types",
            "image/png,image/jpeg,image/gif,application/pdf,"
            "text/plain,text/markdown,text/csv,application/json",
        )

    @pytest.mark.asyncio
    async def test_svg_declared_as_text_plain_rejected_415(self, service, db, workspace_id):
        """The headline bypass: .svg (→ image/svg+xml, NOT allowed) declared as
        text/plain (allowed) must be rejected, not stored mislabeled."""
        with pytest.raises(UnsupportedMediaTypeError) as exc_info:
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="2026-06-08-diagram.svg",
                content_type="text/plain",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        err = exc_info.value
        assert err.status_code == 415
        assert err.error_code == "MEDIA-001"
        # Both the declared and the inferred value are echoed (#961 acceptance).
        assert err.details["content_type"] == "text/plain"
        assert err.details["inferred_content_type"] == "image/svg+xml"
        # Rejection happens before workspace load — no DB execute, no R2 reserve.
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_declared_extension_mismatch_among_allowed_rejected(
        self, service, db, workspace_id
    ):
        """A .png (→ image/png, allowed) declared as text/plain (allowed) is
        still a mismatch — the declared value does not match the extension."""
        with pytest.raises(UnsupportedMediaTypeError) as exc_info:
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="photo.png",
                content_type="text/plain",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        err = exc_info.value
        assert err.status_code == 415
        assert err.details["content_type"] == "text/plain"
        assert err.details["inferred_content_type"] == "image/png"

    @pytest.mark.asyncio
    async def test_uppercase_extension_is_normalized(self, service, db, workspace_id):
        """Extension matching is case-insensitive (.SVG resolves like .svg)."""
        with pytest.raises(UnsupportedMediaTypeError) as exc_info:
            await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="EVIL.SVG",
                content_type="image/png",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        # Pin the reason: it must be the extension-consistency layer (.SVG →
        # image/svg+xml), not an unrelated rejection that happens to also 415.
        assert exc_info.value.details["inferred_content_type"] == "image/svg+xml"

    @pytest.mark.asyncio
    async def test_matching_extension_passes(self, service, db, workspace_id):
        """.png declared image/png — extension and declared agree → allowed."""
        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=True):
            out = await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="photo.png",
                content_type="image/png",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        assert isinstance(out, ReserveResult)

    @pytest.mark.asyncio
    async def test_unknown_extension_falls_back_to_declared_allowlist(
        self, service, db, workspace_id
    ):
        """.md → mimetypes returns None: the consistency layer is skipped and
        the declared allow-list governs. Guards against false-positives on
        extensions Python's mimetypes registry does not know (.md, .webp)."""
        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=True):
            out = await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="notes.md",
                content_type="text/markdown",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        assert isinstance(out, ReserveResult)

    @pytest.mark.asyncio
    async def test_no_extension_falls_back_to_declared_allowlist(self, service, db, workspace_id):
        """A filename with no extension → None inferred → declared allow-list
        governs (e.g. an uploaded "README" declared text/plain)."""
        ws = _make_workspace(workspace_id)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ws)
        db.execute.return_value = result

        with _patch_quota_reserve(succeed=True):
            out = await service.reserve_upload(
                workspace_id=workspace_id,
                created_by="u",
                filename="README",
                content_type="text/plain",
                size_bytes=1024,
                sha256=VALID_SHA,
            )
        assert isinstance(out, ReserveResult)
