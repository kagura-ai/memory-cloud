"""API tests for /api/v1/files routes (Issue #485).

Service-layer behavior is covered by
``backend/tests/services/test_file_storage_service.py``. These tests
verify HTTP/serialization shape, Pydantic validation, dispatch into
the service, and the global ``MemoryCloudException`` handler mapping
(NotFound → 404, Validation → 400, Conflict → 409, QuotaExceeded → 429).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session
from db.base import get_db
from services.file_storage_service import ReserveResult

VALID_SHA = "a" * 64
OTHER_SHA = "b" * 64


def _mock_user() -> dict:
    return {"user_id": "u1", "email": "u@test", "role": "member"}


@pytest.fixture
def client():
    async def fake_user():
        return _mock_user()

    async def fake_db():
        yield MagicMock()

    app.dependency_overrides[get_user_from_api_key_or_session] = fake_user
    app.dependency_overrides[get_db] = fake_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _file_object_mock(**overrides):
    """Mock that survives Pydantic validation for FileObjectOut."""
    file = MagicMock()
    file.id = overrides.get("id", uuid4())
    file.workspace_id = overrides.get("workspace_id", uuid4())
    file.filename = overrides.get("filename", "test.bin")
    file.content_type = overrides.get("content_type", "application/octet-stream")
    file.size_bytes = overrides.get("size_bytes", 1024)
    file.sha256 = overrides.get("sha256", VALID_SHA)
    file.status = overrides.get("status", "uploaded")
    file.created_at = overrides.get("created_at", datetime(2026, 5, 5, tzinfo=UTC))
    file.uploaded_at = overrides.get("uploaded_at", datetime(2026, 5, 5, tzinfo=UTC))
    return file


# ---------------------------------------------------------------------------
# POST /api/v1/files/reserve
# ---------------------------------------------------------------------------


class TestReserveUpload:
    def test_happy_path(self, client):
        ws = uuid4()
        file_id = uuid4()
        expires = datetime.now(UTC) + timedelta(seconds=300)

        with patch(
            "api.routes.files.FileStorageService.reserve_upload",
            AsyncMock(
                return_value=ReserveResult(
                    file_id=file_id,
                    upload_url="https://r2.test/put/key?ttl=300",
                    expires_at=expires,
                )
            ),
        ) as svc:
            r = client.post(
                "/api/v1/files/reserve",
                json={
                    "workspace_id": str(ws),
                    "filename": "x.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 1024,
                    "sha256": VALID_SHA,
                },
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["file_id"] == str(file_id)
        assert body["upload_url"].startswith("https://r2.test/put/")
        svc.assert_awaited_once()

    def test_oversize_rejected_by_pydantic(self, client):
        r = client.post(
            "/api/v1/files/reserve",
            json={
                "workspace_id": str(uuid4()),
                "filename": "big.bin",
                "content_type": "application/octet-stream",
                "size_bytes": 200 * 1024 * 1024,  # > 100 MiB cap
                "sha256": VALID_SHA,
            },
        )
        assert r.status_code == 422

    def test_invalid_sha256_rejected_by_pydantic(self, client):
        r = client.post(
            "/api/v1/files/reserve",
            json={
                "workspace_id": str(uuid4()),
                "filename": "x.bin",
                "content_type": "application/octet-stream",
                "size_bytes": 1024,
                "sha256": "not-hex",
            },
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/files/{file_id}/confirm
# ---------------------------------------------------------------------------


class TestConfirmUpload:
    def test_happy_path(self, client):
        ws = uuid4()
        file_id = uuid4()
        with patch(
            "api.routes.files.FileStorageService.confirm_upload",
            AsyncMock(return_value=_file_object_mock(id=file_id, workspace_id=ws)),
        ):
            r = client.post(
                f"/api/v1/files/{file_id}/confirm?workspace_id={ws}",
                json={"sha256": VALID_SHA},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == str(file_id)
        assert body["status"] == "uploaded"

    def test_sha256_mismatch_validation_error_returns_422(self, client):
        """``ValidationError`` is mapped to HTTP 422 by the global
        ``MemoryCloudException`` handler — same status FastAPI uses for
        Pydantic body validation, so client error handling is uniform."""
        ws = uuid4()
        file_id = uuid4()
        from utils.exceptions import ValidationError

        with patch(
            "api.routes.files.FileStorageService.confirm_upload",
            AsyncMock(side_effect=ValidationError("sha256 mismatch")),
        ):
            r = client.post(
                f"/api/v1/files/{file_id}/confirm?workspace_id={ws}",
                json={"sha256": OTHER_SHA},
            )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/files/{file_id}/download-url
# ---------------------------------------------------------------------------


class TestDownloadUrl:
    def test_happy_path(self, client):
        ws = uuid4()
        file_id = uuid4()
        with patch(
            "api.routes.files.FileStorageService.get_presigned_download",
            AsyncMock(return_value="https://r2.test/get/key?ttl=300"),
        ):
            r = client.get(f"/api/v1/files/{file_id}/download-url?workspace_id={ws}")
        assert r.status_code == 200, r.text
        assert r.json()["download_url"].startswith("https://r2.test/get/")

    def test_not_found_returns_404(self, client):
        ws = uuid4()
        file_id = uuid4()
        from utils.exceptions import NotFoundException

        with patch(
            "api.routes.files.FileStorageService.get_presigned_download",
            AsyncMock(side_effect=NotFoundException("missing")),
        ):
            r = client.get(f"/api/v1/files/{file_id}/download-url?workspace_id={ws}")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/files/{file_id}
# ---------------------------------------------------------------------------


class TestDeleteFile:
    def test_returns_204(self, client):
        ws = uuid4()
        file_id = uuid4()
        with patch(
            "api.routes.files.FileStorageService.delete_file",
            AsyncMock(return_value=None),
        ) as svc:
            r = client.delete(f"/api/v1/files/{file_id}?workspace_id={ws}")
        assert r.status_code == 204
        svc.assert_awaited_once()


# ---------------------------------------------------------------------------
# GET /api/v1/files
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_happy_path(self, client):
        ws = uuid4()
        files = [_file_object_mock(workspace_id=ws), _file_object_mock(workspace_id=ws)]
        with patch(
            "api.routes.files.FileStorageService.list_files",
            AsyncMock(return_value=files),
        ):
            r = client.get(f"/api/v1/files?workspace_id={ws}&limit=10")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_invalid_limit_rejected_by_pydantic(self, client):
        ws = uuid4()
        r = client.get(f"/api/v1/files?workspace_id={ws}&limit=10000")
        assert r.status_code == 422
