"""API tests for the deprecated /api/v1/attachments/* surface (Issue #555).

After #485 (R2 file storage Phase 1), every attachments endpoint returns
HTTP 410 Gone with RFC 8594 Sunset/Deprecation/Link headers. These tests
pin the deprecation contract: status code, headers, body envelope, and the
auth gate (auth must fire BEFORE the 410 so the warn log can record
``user_id`` for straggler-client follow-up).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session

# Stable IDs across all parametrized tests — pytest IDs use the readable
# verb labels below, not these UUIDs, so collisions or content don't matter.
_MEM_ID = str(uuid4())
_ATT_ID = str(uuid4())

_ENDPOINTS = [
    ("POST", f"/api/v1/attachments/memories/{_MEM_ID}"),
    ("GET", f"/api/v1/attachments/memories/{_MEM_ID}"),
    ("GET", f"/api/v1/attachments/{_ATT_ID}"),
    ("DELETE", f"/api/v1/attachments/{_ATT_ID}"),
]
_ENDPOINT_IDS = ["upload", "list", "download", "delete"]


def _mock_user() -> dict:
    return {"user_id": "u1", "email": "u@test", "role": "member"}


@pytest.fixture
def client():
    async def fake_user():
        return _mock_user()

    app.dependency_overrides[get_user_from_api_key_or_session] = fake_user
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client():
    """No auth override — the real dependency runs and rejects the request.

    Clear ``dependency_overrides`` BEFORE yielding too, so a leaked
    override from a prior failing test cannot mask the real auth
    dependency and turn this fixture into a falsely-passing one.
    """
    app.dependency_overrides.clear()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestDeprecationContract:
    """Every retired endpoint emits the same 410 envelope and headers."""

    @pytest.mark.parametrize("method,path", _ENDPOINTS, ids=_ENDPOINT_IDS)
    def test_410_contract(self, client, method, path):
        response = client.request(method, path)

        assert response.status_code == 410
        assert response.headers.get("Sunset") == "Wed, 13 May 2026 00:00:00 GMT"
        assert response.headers.get("Deprecation") == "true"
        assert response.headers.get("Link") == '</api/v1/files/>; rel="successor-version"'

        body = response.json()
        assert body["error"] == "RES-004"
        assert "/api/v1/attachments/* has been retired" in body["message"]
        assert body["details"]["successor"] == "/api/v1/files/"


class TestAuthGate:
    """Auth fires before 410 — without credentials we get 401, not 410.

    The gate matters because the 410 handler logs ``user_id`` for
    straggler-client follow-up; if auth were stripped, the log would
    record ``None`` for every legacy SDK request.
    """

    @pytest.mark.parametrize("method,path", _ENDPOINTS, ids=_ENDPOINT_IDS)
    def test_unauthenticated_request_is_rejected(self, unauth_client, method, path):
        response = unauth_client.request(method, path)
        # The real auth dependency rejects with 401 before the route body runs;
        # the 410 (and its log) never fire when there is no caller identity.
        assert response.status_code == 401


class TestOpenAPI:
    """Each retired route is still listed in OpenAPI with deprecated=True."""

    _HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")

    def test_routes_marked_deprecated(self, client):
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        for path_template in (
            "/api/v1/attachments/memories/{memory_id}",
            "/api/v1/attachments/{attachment_id}",
        ):
            assert path_template in paths, f"missing in OpenAPI: {path_template}"
            for method in self._HTTP_METHODS:
                op = paths[path_template].get(method)
                if op is None:
                    continue
                assert op.get("deprecated") is True, (
                    f"{method.upper()} {path_template}: deprecated marker missing"
                )
