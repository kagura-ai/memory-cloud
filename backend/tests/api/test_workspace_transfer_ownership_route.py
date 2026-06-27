"""Route-level tests for POST /api/v1/workspaces/{id}/transfer-ownership (#1094).

The transactional transfer logic (row lock, single-owner invariant, epoch bump,
audit) lives in WorkspaceOwnershipService and is covered by the integration
suite against a real DB. These tests pin the route contract: session-only auth,
owner-only gate bound to the PATH workspace_id, the service-error → HTTP-status
mapping, and the response shape — all DB-free via dependency overrides + a
patched service.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.routes import workspaces as route_mod
from auth.dependencies import require_session_auth
from db.base import get_db
from services.workspace_ownership_service import OwnershipTransferResult
from utils.exceptions import (
    AuthorizationError,
    BadRequestError,
    ConflictError,
    MemoryCloudException,
    NotFoundException,
)

WS = uuid4()
OWNER = "owner-1"
TARGET = "member-2"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(route_mod.router, prefix="/api/v1/workspaces")

    async def _mc_handler(_request, exc: MemoryCloudException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": exc.error_code, "message": exc.message},
        )

    app.add_exception_handler(MemoryCloudException, _mc_handler)
    return app


@pytest.fixture
def owner_client():
    """Client where require_session_auth yields the owner and the owner gate passes."""
    app = _build_app()

    async def _mock_session():
        return {"user_id": OWNER, "email": "owner@test.com", "current_workspace_id": uuid4()}

    async def _mock_db():
        yield MagicMock()

    app.dependency_overrides[require_session_auth] = _mock_session
    app.dependency_overrides[get_db] = _mock_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _result(changed: bool, epoch: int) -> OwnershipTransferResult:
    return OwnershipTransferResult(
        workspace_id=WS,
        previous_owner_id=OWNER,
        new_owner_id=TARGET,
        ownership_epoch=epoch,
        changed=changed,
    )


def _owner_ok():
    perm = MagicMock()
    perm.check_workspace_owner = AsyncMock(return_value=MagicMock())
    return perm


class TestTransferOwnerHappyPath:
    def test_owner_transfers_returns_200_and_shape(self, owner_client):
        perm = _owner_ok()
        svc = MagicMock()
        svc.transfer_ownership = AsyncMock(return_value=_result(changed=True, epoch=1))
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "WorkspaceOwnershipService", return_value=svc),
        ):
            resp = owner_client.post(
                f"/api/v1/workspaces/{WS}/transfer-ownership",
                json={"target_user_id": TARGET},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {
            "workspace_id": str(WS),
            "previous_owner_id": OWNER,
            "new_owner_id": TARGET,
            "ownership_epoch": 1,
            "changed": True,
        }
        # Owner gate is bound to the PATH workspace id; service gets the same id,
        # the authenticated caller as current_owner, and the body target.
        assert perm.check_workspace_owner.await_args.args[1] == WS
        call = svc.transfer_ownership.await_args.kwargs
        assert call["workspace_id"] == WS
        assert call["current_owner_id"] == OWNER
        assert call["target_user_id"] == TARGET
        # The audit actor is the authenticated session, not the body.
        assert call["performed_by_email"] == "owner@test.com"

    def test_idempotent_noop_reports_changed_false(self, owner_client):
        perm = _owner_ok()
        svc = MagicMock()
        svc.transfer_ownership = AsyncMock(return_value=_result(changed=False, epoch=3))
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "WorkspaceOwnershipService", return_value=svc),
        ):
            resp = owner_client.post(
                f"/api/v1/workspaces/{WS}/transfer-ownership",
                json={"target_user_id": TARGET},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["changed"] is False
        assert body["ownership_epoch"] == 3


class TestTransferOwnerOnly:
    def test_non_owner_gets_403(self, owner_client):
        perm = MagicMock()
        perm.check_workspace_owner = AsyncMock(
            side_effect=AuthorizationError(reason="role_too_low")
        )
        with patch.object(route_mod, "PermissionService", return_value=perm):
            resp = owner_client.post(
                f"/api/v1/workspaces/{WS}/transfer-ownership",
                json={"target_user_id": TARGET},
            )
        assert resp.status_code == 403, resp.text

    @pytest.mark.parametrize(
        "bearer", ["kagura_live_testkey", "opaque-oauth-token-xyz"], ids=["api_key", "oauth"]
    )
    def test_any_bearer_credential_is_rejected_403(self, bearer):
        # Real require_session_auth: a sensitive governance action must reject any
        # Bearer credential before the owner check or any mutation.
        app = _build_app()

        async def _mock_db():
            yield MagicMock()

        app.dependency_overrides[get_db] = _mock_db
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                f"/api/v1/workspaces/{WS}/transfer-ownership",
                json={"target_user_id": TARGET},
                headers={"Authorization": f"Bearer {bearer}"},
            )
        assert resp.status_code == 403, resp.text


class TestTransferServiceErrorMapping:
    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            (BadRequestError("not a member", error_code="WS-OWNER-001"), 400),
            (ConflictError("ownership changed concurrently"), 409),
            (NotFoundException("Workspace"), 404),
        ],
        ids=["target_not_member_400", "concurrent_conflict_409", "missing_workspace_404"],
    )
    def test_service_errors_map_to_status(self, owner_client, exc, status):
        perm = _owner_ok()
        svc = MagicMock()
        svc.transfer_ownership = AsyncMock(side_effect=exc)
        with (
            patch.object(route_mod, "PermissionService", return_value=perm),
            patch.object(route_mod, "WorkspaceOwnershipService", return_value=svc),
        ):
            resp = owner_client.post(
                f"/api/v1/workspaces/{WS}/transfer-ownership",
                json={"target_user_id": TARGET},
            )
        assert resp.status_code == status, resp.text


class TestTransferValidation:
    def test_missing_target_is_422(self, owner_client):
        with patch.object(route_mod, "PermissionService", return_value=_owner_ok()):
            resp = owner_client.post(f"/api/v1/workspaces/{WS}/transfer-ownership", json={})
        assert resp.status_code == 422

    def test_empty_target_is_422(self, owner_client):
        with patch.object(route_mod, "PermissionService", return_value=_owner_ok()):
            resp = owner_client.post(
                f"/api/v1/workspaces/{WS}/transfer-ownership",
                json={"target_user_id": ""},
            )
        assert resp.status_code == 422

    def test_non_uuid_workspace_is_422(self, owner_client):
        with patch.object(route_mod, "PermissionService", return_value=_owner_ok()):
            resp = owner_client.post(
                "/api/v1/workspaces/not-a-uuid/transfer-ownership",
                json={"target_user_id": TARGET},
            )
        assert resp.status_code == 422
