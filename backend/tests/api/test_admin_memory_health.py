"""Route-level tests for the deprecated GET /api/v1/admin/memory-health alias.

#2764 moved the report to ``GET /api/v1/memory-health`` (see
``test_memory_health.py``). The admin path stays for one release as a
deprecated alias of the same handler: still behind the system-admin gate,
answering a system admin with the same document as the new path, marked
``deprecated`` in the OpenAPI document with a description naming its
replacement.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_current_user, require_admin, require_session_auth
from db.base import get_db
from tests.api.test_memory_health import CANNED_BREAKDOWN, CANNED_DETAIL

SERVICE = "api.routes.memory_health.MemoryHealthService"
ALIAS = "/api/v1/admin/memory-health"
NEW = "/api/v1/memory-health"

_CTX = CANNED_DETAIL["context_id"]


def _admin_user() -> dict:
    return {"user_id": "admin_user_1", "email": "admin@test.com", "role": "admin"}


async def _mock_get_db():
    yield AsyncMock()


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _as_admin():
    async def admin():
        return _admin_user()

    app.dependency_overrides[require_admin] = admin
    app.dependency_overrides[require_session_auth] = admin
    app.dependency_overrides[get_db] = _mock_get_db


class TestDeprecatedAdminAlias:
    def test_non_admin_gets_403(self, client) -> None:
        async def member_user():
            return {"user_id": "u1", "email": "member@test.com", "role": "member"}

        app.dependency_overrides[get_current_user] = member_user
        app.dependency_overrides[get_db] = _mock_get_db
        called = AsyncMock()
        with patch(f"{SERVICE}.build_breakdown", new=called):
            resp = client.get(ALIAS)
        assert resp.status_code == 403
        called.assert_not_awaited()

    def test_breakdown_is_the_same_document_as_the_new_path(self, client) -> None:
        _as_admin()
        with patch(
            f"{SERVICE}.build_breakdown", new=AsyncMock(return_value=CANNED_BREAKDOWN)
        ) as mocked:
            alias = client.get(ALIAS)
            new = client.get(NEW)

        assert alias.status_code == 200
        assert alias.json() == new.json()
        assert alias.json()["contexts"][0]["context_id"] == _CTX
        assert mocked.await_args.args[0] == "admin_user_1"

    def test_detail_is_the_same_document_as_the_new_path(self, client) -> None:
        _as_admin()
        with patch(
            f"{SERVICE}.build_context_report", new=AsyncMock(return_value=CANNED_DETAIL)
        ) as mocked:
            alias = client.get(f"{ALIAS}?context_id={_CTX}")
            new = client.get(f"{NEW}?context_id={_CTX}")

        assert alias.status_code == 200
        assert alias.json() == new.json()
        assert mocked.await_args.args[1] == uuid.UUID(_CTX)

    def test_unattributed_sentinel_maps_to_null_scope(self, client) -> None:
        _as_admin()
        canned = {**CANNED_DETAIL, "context_id": None, "context_name": None}
        with patch(f"{SERVICE}.build_context_report", new=AsyncMock(return_value=canned)) as mocked:
            resp = client.get(f"{ALIAS}?context_id=unattributed")

        assert resp.status_code == 200
        assert resp.json()["context_id"] is None
        assert mocked.await_args.args[1] is None

    def test_unowned_context_gets_uniform_404(self, client) -> None:
        _as_admin()
        with patch(f"{SERVICE}.build_context_report", new=AsyncMock(return_value=None)):
            resp = client.get(f"{ALIAS}?context_id={uuid.uuid4()}")

        assert resp.status_code == 404
        assert "Context not found" in resp.text

    def test_malformed_context_id_gets_422(self, client) -> None:
        _as_admin()
        resp = client.get(f"{ALIAS}?context_id=not-a-uuid")
        assert resp.status_code == 422

    def test_missing_user_id_yields_health_001(self, client) -> None:
        async def admin_without_id():
            return {"email": "admin@test.com", "role": "admin"}

        app.dependency_overrides[require_admin] = admin_without_id
        app.dependency_overrides[get_db] = _mock_get_db

        resp = client.get(ALIAS)
        assert resp.status_code == 500
        assert "HEALTH-001" in resp.text

    def test_openapi_marks_the_alias_deprecated_and_names_the_new_path(self) -> None:
        operation = app.openapi()["paths"][ALIAS]["get"]
        assert operation["deprecated"] is True
        assert NEW in operation["description"]
