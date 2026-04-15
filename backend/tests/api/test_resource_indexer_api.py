"""Tests for the GET /api/v1/resources/{id}/indexer-status API.

Issue #326. These tests focus on the route contract — request shape,
response shape, and the cross-tenant 404 invariant — using
``dependency_overrides`` to mock auth and ``unittest.mock.patch`` to
stub out the resolver / service helpers. The aim is fast, deterministic
coverage of the API layer; deeper data-layer behavior of
``services.resource_indexer.get_indexer_status_for_context`` is
exercised separately by service-level tests.
"""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session

WORKSPACE_A = uuid4()
WORKSPACE_B = uuid4()


def _mock_session_user(workspace_id=WORKSPACE_A) -> dict:
    return {
        "user_id": "test_user",
        "email": "test@example.com",
        "role": "user",
        "current_workspace_id": workspace_id,
    }


@pytest.fixture
def client_workspace_a():
    user = _mock_session_user(WORKSPACE_A)

    async def mock_auth(request=None, api_key=None, db=None):
        return user

    app.dependency_overrides[get_user_from_api_key_or_session] = mock_auth
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client():
    """No auth override — endpoint must require credentials."""
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _make_context(*, workspace_id=WORKSPACE_A, resource_id="ec_products"):
    """Lightweight Context stand-in for the resolver mock.

    The route only reads ``id``, ``workspace_id``, and ``resource_id``, then
    hands the object to the service function (also mocked). A SimpleNamespace
    avoids SQLAlchemy descriptor machinery which requires a Session bind.
    """
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        resource_id=resource_id,
    )


# ============================================================================
# Auth / route plumbing
# ============================================================================


class TestAuthentication:
    def test_unauth_returns_401(self, unauth_client):
        # Sanity guard against the dependency being accidentally removed.
        resp = unauth_client.get("/api/v1/resources/anything/indexer-status")
        assert resp.status_code == 401


# ============================================================================
# Response contract
# ============================================================================


class TestResponseContract:
    def test_returns_state_null_when_indexer_never_ran(self, client_workspace_a):
        ctx = _make_context()

        async def fake_resolver(self, **kw):
            return ctx

        async def fake_service(db, context, **kw):
            return {
                "resource_id": context.resource_id,
                "state": None,
                "recent_events": [],
            }

        with (
            patch(
                "services.permission_service.PermissionService.resolve_resource_by_slug",
                new=fake_resolver,
            ),
            patch(
                "api.routes.resource_indexer.get_indexer_status_for_context",
                new=fake_service,
            ),
        ):
            resp = client_workspace_a.get("/api/v1/resources/ec_products/indexer-status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["resource_id"] == "ec_products"
        assert body["state"] is None
        assert body["recent_events"] == []

    def test_returns_full_state_when_indexer_has_run(self, client_workspace_a):
        ctx = _make_context()

        async def fake_resolver(self, **kw):
            return ctx

        async def fake_service(db, context, **kw):
            return {
                "resource_id": context.resource_id,
                "state": {
                    "job_status": "idle",
                    "last_run_at": "2026-04-15T00:00:00Z",
                    "next_run_at": None,
                    "active_version": 1,
                    "last_offset": 100,
                    "lag_seconds": 60.0,
                    "metrics": {
                        "applied_upserts": 10,
                        "applied_deletes": 1,
                        "errors": 0,
                        "skipped_reason": None,
                    },
                },
                "recent_events": [
                    {
                        "id": 1,
                        "op": "upsert",
                        "doc_id": "d-001",
                        "version": 1,
                        "created_at": "2026-04-15T00:00:00Z",
                    }
                ],
            }

        with (
            patch(
                "services.permission_service.PermissionService.resolve_resource_by_slug",
                new=fake_resolver,
            ),
            patch(
                "api.routes.resource_indexer.get_indexer_status_for_context",
                new=fake_service,
            ),
        ):
            resp = client_workspace_a.get("/api/v1/resources/ec_products/indexer-status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["state"]["job_status"] == "idle"
        assert body["state"]["metrics"]["applied_upserts"] == 10
        assert len(body["recent_events"]) == 1
        assert body["recent_events"][0]["op"] == "upsert"

    def test_invalid_job_status_enum_is_rejected_at_response_boundary(self, client_workspace_a):
        """Pydantic must refuse a job_status outside the Literal enum.

        Pins the contract that adding a new server-side state requires also
        extending ``IndexerJobStatus`` — silent acceptance would let an
        unknown value reach the UI and crash the Badge variant lookup.
        """
        ctx = _make_context()

        async def fake_resolver(self, **kw):
            return ctx

        async def fake_service(db, context, **kw):
            return {
                "resource_id": context.resource_id,
                "state": {
                    "job_status": "paused",  # not in the Literal enum
                    "last_run_at": None,
                    "next_run_at": None,
                    "active_version": 1,
                    "last_offset": 0,
                    "lag_seconds": None,
                    "metrics": {
                        "applied_upserts": 0,
                        "applied_deletes": 0,
                        "errors": 0,
                        "skipped_reason": None,
                    },
                },
                "recent_events": [],
            }

        with (
            patch(
                "services.permission_service.PermissionService.resolve_resource_by_slug",
                new=fake_resolver,
            ),
            patch(
                "api.routes.resource_indexer.get_indexer_status_for_context",
                new=fake_service,
            ),
        ):
            resp = client_workspace_a.get("/api/v1/resources/ec_products/indexer-status")

        # FastAPI surfaces response_model violations as 500 with raise_server_exceptions=False.
        assert resp.status_code == 500


# ============================================================================
# Cross-tenant isolation (CSO Test 1-2)
# ============================================================================


class TestIsolation:
    def test_unknown_slug_returns_404(self, client_workspace_a):
        async def fake_resolver(self, **kw):
            raise HTTPException(status_code=404, detail="Resource not found")

        with patch(
            "services.permission_service.PermissionService.resolve_resource_by_slug",
            new=fake_resolver,
        ):
            resp = client_workspace_a.get("/api/v1/resources/does-not-exist/indexer-status")

        assert resp.status_code == 404
        assert resp.json() == {"detail": "Resource not found"}

    def test_slug_in_other_workspace_returns_404_with_identical_body(self, client_workspace_a):
        """Cross-tenant existence must not leak.

        Both an unknown slug and a slug that exists only in another workspace
        must hit the same HTTPException(404) path, producing byte-identical
        response bodies. A different body would let an attacker enumerate
        slugs via a body-shape oracle.
        """

        async def fake_resolver(self, **kw):
            raise HTTPException(status_code=404, detail="Resource not found")

        with patch(
            "services.permission_service.PermissionService.resolve_resource_by_slug",
            new=fake_resolver,
        ):
            resp_other_ws = client_workspace_a.get(
                "/api/v1/resources/billing-events/indexer-status"
            )
            resp_unknown = client_workspace_a.get("/api/v1/resources/totally-fake/indexer-status")

        assert resp_other_ws.status_code == resp_unknown.status_code == 404
        assert resp_other_ws.json() == resp_unknown.json()


# ============================================================================
# OpenAPI shape pin
# ============================================================================


class TestOpenAPISnapshot:
    """Pin the OpenAPI shape so backend changes that desync the hand-written
    TypeScript client become visible in CI. When this assertion needs an
    update, the frontend `IndexerStatusResponse` types must be updated in
    the same change set."""

    EXPECTED_TOP_LEVEL_FIELDS = {"resource_id", "state", "recent_events"}
    EXPECTED_STATE_FIELDS = {
        "job_status",
        "last_run_at",
        "next_run_at",
        "active_version",
        "last_offset",
        "lag_seconds",
        "metrics",
    }
    EXPECTED_METRICS_FIELDS = {
        "applied_upserts",
        "applied_deletes",
        "errors",
        "skipped_reason",
    }

    def test_endpoint_is_listed_in_openapi(self, client_workspace_a):
        spec = client_workspace_a.get("/openapi.json").json()
        assert "/api/v1/resources/{resource_id}/indexer-status" in spec["paths"]

    def test_response_schema_shape_is_stable(self, client_workspace_a):
        spec = client_workspace_a.get("/openapi.json").json()
        schemas = spec["components"]["schemas"]

        assert (
            set(schemas["IndexerStatusResponse"]["properties"].keys())
            == self.EXPECTED_TOP_LEVEL_FIELDS
        )
        assert set(schemas["IndexerState"]["properties"].keys()) == self.EXPECTED_STATE_FIELDS
        assert (
            set(schemas["IndexerStateMetrics"]["properties"].keys()) == self.EXPECTED_METRICS_FIELDS
        )

    def test_job_status_enum_is_pinned(self, client_workspace_a):
        """Pydantic v2 inlines a bare ``Literal`` into the property schema
        rather than producing a named component. Walk into IndexerState's
        job_status property and read the inlined enum directly."""
        spec = client_workspace_a.get("/openapi.json").json()
        schemas = spec["components"]["schemas"]
        job_status_prop = schemas["IndexerState"]["properties"]["job_status"]

        assert "enum" in job_status_prop, (
            "job_status property no longer carries an inlined enum — "
            "Literal may have been replaced. Update both this assertion and "
            "the IndexerJobStatus type in `frontend/src/lib/api/resources.ts`."
        )
        assert set(job_status_prop["enum"]) == {
            "idle",
            "queued",
            "running",
            "failed",
        }
