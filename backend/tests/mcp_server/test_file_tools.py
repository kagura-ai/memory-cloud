"""Tests for MCP file storage tool handlers (Issue #485).

Service-layer behaviour is covered by
``tests/services/test_file_storage_service.py``. These tests verify
the MCP-specific shape:

- argument validation / missing-fields error vocabulary
- workspace_id resolution (arg override vs auth fallback)
- error-vocabulary mapping (MCP returns ``error: validation_error``,
  ``not_found``, ``conflict``, ``quota_exceeded`` — same vocabulary
  as the REST layer's HTTP status codes)
- success response shape (file_id / upload_url / etc.)

Patches FileStorageService at the class level (mirrors
``test_resource_ingest_quota.py``'s pattern for MCP handler tests).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.files import (
    handle_complete_file_upload,
    handle_delete_file,
    handle_get_file_download_url,
    handle_init_file_upload,
    handle_list_files,
)
from services.file_storage_service import ReserveResult
from utils.exceptions import (
    ConflictError,
    NotFoundException,
    QuotaExceededError,
    UnsupportedMediaTypeError,
    ValidationError,
)

VALID_SHA = "a" * 64
USER_ID = "u1"


def _patch_get_db():
    """Make ``async for db in get_db()`` yield a MagicMock once."""
    db = MagicMock()

    async def _aiter():
        yield db

    return patch("db.base.get_db", return_value=_aiter()), db


def _patch_viewer_check_pass():
    """Patch BOTH gates (write-tool _check_viewer_permission and
    read-tool _check_workspace_membership) — tests use this helper as
    a one-stop "auth passes" override regardless of which gate the
    handler under test is using. Read tools (get_file_download_url,
    list_files) switched to ``_check_workspace_membership`` in
    Copilot loop 3 (PR #551) so viewers can read; without patching
    that helper too, those tests would skip the gate via the
    membership-only check returning a real error."""
    from contextlib import contextmanager

    @contextmanager
    def _both():
        with (
            patch(
                "mcp_server.tools.files._check_viewer_permission",
                AsyncMock(return_value=None),
            ),
            patch(
                "mcp_server.tools.files._check_workspace_membership",
                AsyncMock(return_value=None),
            ),
        ):
            yield

    return _both()


def _payload(text_list) -> dict:
    """Decode the JSON payload of a TextContent response."""
    assert len(text_list) == 1
    return json.loads(text_list[0].text)


# ---------------------------------------------------------------------------
# init_file_upload
# ---------------------------------------------------------------------------


class TestInitFileUpload:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        ws = uuid4()
        file_id = uuid4()
        expires = datetime.now(UTC) + timedelta(seconds=300)

        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.reserve_upload",
                AsyncMock(
                    return_value=ReserveResult(
                        file_id=file_id,
                        upload_url="https://r2.test/put/key",
                        expires_at=expires,
                    )
                ),
            ),
        ):
            out = await handle_init_file_upload(
                {
                    "filename": "x.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 1024,
                    "sha256": VALID_SHA,
                },
                user_id=USER_ID,
                workspace_id=ws,
            )

        body = _payload(out)
        assert body["file_id"] == str(file_id)
        assert body["upload_url"] == "https://r2.test/put/key"

    @pytest.mark.asyncio
    async def test_missing_field_returns_error(self):
        out = await handle_init_file_upload(
            {"filename": "x.pdf"},
            user_id=USER_ID,
            workspace_id=uuid4(),
        )
        body = _payload(out)
        assert body["error"] == "missing_fields"

    @pytest.mark.asyncio
    async def test_no_workspace_returns_error(self):
        out = await handle_init_file_upload(
            {
                "filename": "x.pdf",
                "content_type": "application/pdf",
                "size_bytes": 1024,
                "sha256": VALID_SHA,
            },
            user_id=USER_ID,
            workspace_id=None,  # No fallback either
        )
        body = _payload(out)
        assert body["error"] == "workspace_required"

    @pytest.mark.asyncio
    async def test_quota_exceeded_maps_to_quota_error(self):
        ws = uuid4()
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.reserve_upload",
                AsyncMock(side_effect=QuotaExceededError("over the limit")),
            ),
        ):
            out = await handle_init_file_upload(
                {
                    "filename": "x.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 1024,
                    "sha256": VALID_SHA,
                },
                user_id=USER_ID,
                workspace_id=ws,
            )
        body = _payload(out)
        assert body["error"] == "quota_exceeded"

    @pytest.mark.asyncio
    async def test_unsupported_media_type_returns_dedicated_vocab(self):
        """Dedicated MCP vocab (``unsupported_media_type``) so SDKs can
        route MIME-rejected uploads without inspecting message text."""
        ws = uuid4()
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.reserve_upload",
                AsyncMock(
                    side_effect=UnsupportedMediaTypeError(
                        content_type="application/x-msdownload",
                        allowed=["image/png", "application/pdf"],
                    )
                ),
            ),
        ):
            out = await handle_init_file_upload(
                {
                    "filename": "evil.exe",
                    "content_type": "application/x-msdownload",
                    "size_bytes": 1024,
                    "sha256": VALID_SHA,
                },
                user_id=USER_ID,
                workspace_id=ws,
            )
        body = _payload(out)
        assert body["error"] == "unsupported_media_type"
        assert "application/x-msdownload" in body["message"]
        # Loop 2: forward exc.details so MCP clients mirror REST 415 body
        # without parsing message text (same pattern as analysis._gate_error_response).
        assert body["content_type"] == "application/x-msdownload"
        assert body["allowed"] == ["application/pdf", "image/png"]

    @pytest.mark.asyncio
    async def test_conflict_maps_to_conflict_error(self):
        ws = uuid4()
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.reserve_upload",
                AsyncMock(side_effect=ConflictError("duplicate sha256")),
            ),
        ):
            out = await handle_init_file_upload(
                {
                    "filename": "x.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 1024,
                    "sha256": VALID_SHA,
                },
                user_id=USER_ID,
                workspace_id=ws,
            )
        body = _payload(out)
        assert body["error"] == "conflict"


# ---------------------------------------------------------------------------
# complete_file_upload
# ---------------------------------------------------------------------------


class TestCompleteFileUpload:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        ws = uuid4()
        file_id = uuid4()

        finalised = MagicMock()
        finalised.id = file_id
        finalised.status = "uploaded"
        finalised.size_bytes = 1024
        finalised.sha256 = VALID_SHA

        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.confirm_upload",
                AsyncMock(return_value=finalised),
            ),
        ):
            out = await handle_complete_file_upload(
                {"file_id": str(file_id), "sha256": VALID_SHA},
                user_id=USER_ID,
                workspace_id=ws,
            )

        body = _payload(out)
        assert body["status"] == "uploaded"
        assert body["file_id"] == str(file_id)

    @pytest.mark.asyncio
    async def test_invalid_file_id_returns_validation_error(self):
        out = await handle_complete_file_upload(
            {"file_id": "not-a-uuid", "sha256": VALID_SHA},
            user_id=USER_ID,
            workspace_id=uuid4(),
        )
        body = _payload(out)
        assert body["error"] == "validation_error"

    @pytest.mark.asyncio
    async def test_sha256_mismatch_validation_error(self):
        ws = uuid4()
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.confirm_upload",
                AsyncMock(side_effect=ValidationError("sha256 mismatch")),
            ),
        ):
            out = await handle_complete_file_upload(
                {"file_id": str(uuid4()), "sha256": VALID_SHA},
                user_id=USER_ID,
                workspace_id=ws,
            )
        body = _payload(out)
        assert body["error"] == "validation_error"


# ---------------------------------------------------------------------------
# get_file_download_url
# ---------------------------------------------------------------------------


class TestGetFileDownloadUrl:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        ws = uuid4()
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.get_presigned_download",
                AsyncMock(return_value="https://r2.test/get/key"),
            ),
        ):
            out = await handle_get_file_download_url(
                {"file_id": str(uuid4())},
                user_id=USER_ID,
                workspace_id=ws,
            )
        body = _payload(out)
        assert body["download_url"] == "https://r2.test/get/key"

    @pytest.mark.asyncio
    async def test_not_found_maps_to_not_found(self):
        ws = uuid4()
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.get_presigned_download",
                AsyncMock(side_effect=NotFoundException("missing")),
            ),
        ):
            out = await handle_get_file_download_url(
                {"file_id": str(uuid4())},
                user_id=USER_ID,
                workspace_id=ws,
            )
        body = _payload(out)
        assert body["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_workspace_id_override_requires_membership(self):
        """Copilot finding on PR #551: an authenticated MCP caller passing
        a foreign ``workspace_id`` MUST be blocked at the membership gate
        before reaching the service."""
        from utils.exceptions import AuthorizationError

        foreign_ws = uuid4()
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            patch(
                # Loop 3: read tools use _check_workspace_membership
                # (membership-only); write tools still use
                # _check_viewer_permission. This test exercises the
                # read tool path so it patches the membership variant.
                "mcp_server.tools.files._check_workspace_membership",
                AsyncMock(
                    return_value=[
                        MagicMock(
                            text='{"status":"error","error":"forbidden","message":"not a member"}'
                        )
                    ]
                ),
            ),
            patch(
                "mcp_server.tools.files.FileStorageService.get_presigned_download",
                AsyncMock(),
            ) as svc,
        ):
            out = await handle_get_file_download_url(
                {"file_id": str(uuid4()), "workspace_id": str(foreign_ws)},
                user_id=USER_ID,
                workspace_id=uuid4(),  # caller's home workspace, different
            )
        # Service NEVER reached — gate fired first.
        svc.assert_not_awaited()
        # The mocked viewer-check error response is returned verbatim.
        assert "forbidden" in out[0].text
        del AuthorizationError  # silence unused-import on the test-class


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------


class TestDeleteFile:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        ws = uuid4()
        file_id = uuid4()
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.delete_file",
                AsyncMock(return_value=None),
            ) as svc,
        ):
            out = await handle_delete_file(
                {"file_id": str(file_id)},
                user_id=USER_ID,
                workspace_id=ws,
            )
        body = _payload(out)
        assert body["deleted"] is True
        assert body["file_id"] == str(file_id)
        svc.assert_awaited_once()


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        ws = uuid4()
        f1 = MagicMock(
            id=uuid4(),
            filename="a.bin",
            content_type="application/octet-stream",
            size_bytes=100,
            sha256=VALID_SHA,
            status="uploaded",
            created_at=datetime.now(UTC),
            uploaded_at=datetime.now(UTC),
        )
        f2 = MagicMock(
            id=uuid4(),
            filename="b.bin",
            content_type="application/octet-stream",
            size_bytes=200,
            sha256="b" * 64,
            status="uploaded",
            created_at=datetime.now(UTC),
            uploaded_at=datetime.now(UTC),
        )
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.list_files",
                AsyncMock(return_value=[f1, f2]),
            ),
        ):
            out = await handle_list_files(
                {"limit": 10},
                user_id=USER_ID,
                workspace_id=ws,
            )
        body = _payload(out)
        assert body["count"] == 2
        assert len(body["files"]) == 2

    @pytest.mark.asyncio
    async def test_invalid_limit_validation_error(self):
        ws = uuid4()
        get_db_patch, _ = _patch_get_db()
        with (
            get_db_patch,
            _patch_viewer_check_pass(),
            patch(
                "mcp_server.tools.files.FileStorageService.list_files",
                AsyncMock(side_effect=ValidationError("limit out of range")),
            ),
        ):
            out = await handle_list_files(
                {"limit": 9999},
                user_id=USER_ID,
                workspace_id=ws,
            )
        body = _payload(out)
        assert body["error"] == "validation_error"


# ---------------------------------------------------------------------------
# Registry / TOOLS_WITHOUT_CONTEXT_ID
# ---------------------------------------------------------------------------


class TestRegistryWiring:
    def test_all_five_tools_in_no_context_set(self):
        """Files don't have a context_id; the dispatch guard MUST not
        reject them for missing context_id."""
        from mcp_server.tools import _TOOLS_WITHOUT_CONTEXT_ID

        for name in (
            "init_file_upload",
            "complete_file_upload",
            "get_file_download_url",
            "delete_file",
            "list_files",
        ):
            assert name in _TOOLS_WITHOUT_CONTEXT_ID, (
                f"{name} missing from _TOOLS_WITHOUT_CONTEXT_ID"
            )

    def test_registry_dispatches_file_handlers(self):
        from mcp_server.tools import _build_registry

        reg = _build_registry()
        assert reg["init_file_upload"] is handle_init_file_upload
        assert reg["complete_file_upload"] is handle_complete_file_upload
        assert reg["get_file_download_url"] is handle_get_file_download_url
        assert reg["delete_file"] is handle_delete_file
        assert reg["list_files"] is handle_list_files

    def test_definitions_include_all_five(self):
        from mcp_server.tools._definitions import get_tool_definitions

        names = {d["name"] for d in get_tool_definitions()}
        for n in (
            "init_file_upload",
            "complete_file_upload",
            "get_file_download_url",
            "delete_file",
            "list_files",
        ):
            assert n in names
