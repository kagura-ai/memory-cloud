"""Route tests for the break-glass admin force-transfer endpoint (#1101).

DB-free: dependency overrides + a patched service. Verifies the system-admin gate,
the required non-empty reason, the path-bound workspace id, the response shape, and
that the displaced PREVIOUS owner is the notification target.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.routes import admin as admin_mod
from auth.dependencies import get_current_user
from db.base import get_db
from services.workspace_ownership_service import OwnershipTransferResult
from utils.exceptions import MemoryCloudException

ADMIN = "admin_user_1"
TARGET = "target_user_1"
PREV = "prev_owner_1"
WS = uuid4()
_PATH = f"/admin/workspaces/{WS}/force-transfer-ownership"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_mod.router)

    async def _mc_handler(_request, exc: MemoryCloudException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": getattr(exc, "error_code", None), "message": str(exc)}},
        )

    app.add_exception_handler(MemoryCloudException, _mc_handler)
    return app


def _client(role: str = "admin") -> TestClient:
    app = _build_app()

    async def _user():
        return {"user_id": ADMIN, "email": "admin@test.com", "role": role}

    async def _db():
        yield MagicMock()

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def _result(changed: bool = True, epoch: int = 1) -> OwnershipTransferResult:
    return OwnershipTransferResult(
        workspace_id=WS,
        previous_owner_id=PREV,
        new_owner_id=TARGET,
        ownership_epoch=epoch,
        changed=changed,
    )


class TestForceTransferRoute:
    def test_admin_force_transfers_returns_200_and_calls_service(self):
        svc = MagicMock()
        svc.force_transfer_ownership = AsyncMock(return_value=_result())
        notify = AsyncMock()
        with (
            patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc),
            patch.object(admin_mod, "_notify_force_transfer_best_effort", notify),
        ):
            resp = _client().post(
                _PATH, json={"target_user_id": TARGET, "reason": "owner unreachable"}
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["new_owner_id"] == TARGET
        assert body["previous_owner_id"] == PREV
        assert body["changed"] is True
        # Bound to the PATH workspace id; actor is the authenticated admin.
        call = svc.force_transfer_ownership.await_args.kwargs
        assert call["workspace_id"] == WS
        assert call["target_user_id"] == TARGET
        assert call["performed_by_user_id"] == ADMIN
        assert call["performed_by_email"] == "admin@test.com"
        assert call["reason"] == "owner unreachable"
        # The DISPLACED previous owner is the notification target.
        notify.assert_awaited_once()
        assert notify.await_args.args[1] == WS
        assert notify.await_args.args[2] == PREV

    def test_non_admin_is_403(self):
        # require_admin rejects role != "admin" before the body runs.
        resp = _client(role="user").post(
            _PATH, json={"target_user_id": TARGET, "reason": "valid reason"}
        )
        assert resp.status_code == 403

    def test_short_reason_is_422(self):
        resp = _client().post(_PATH, json={"target_user_id": TARGET, "reason": "ab"})
        assert resp.status_code == 422

    def test_missing_reason_is_422(self):
        resp = _client().post(_PATH, json={"target_user_id": TARGET})
        assert resp.status_code == 422

    def test_no_notification_on_idempotent_noop(self):
        svc = MagicMock()
        svc.force_transfer_ownership = AsyncMock(return_value=_result(changed=False, epoch=0))
        notify = AsyncMock()
        with (
            patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc),
            patch.object(admin_mod, "_notify_force_transfer_best_effort", notify),
        ):
            resp = _client().post(_PATH, json={"target_user_id": TARGET, "reason": "noop reason"})
        assert resp.status_code == 200
        notify.assert_not_awaited()


class TestNotifyForceTransferBestEffort:
    @pytest.mark.asyncio
    async def test_resolves_previous_owner_email_and_sends(self):
        db = MagicMock()
        email_res = MagicMock()
        email_res.scalar_one_or_none.return_value = "prev@owner.com"
        name_res = MagicMock()
        name_res.scalar_one_or_none.return_value = "My WS"
        db.execute = AsyncMock(side_effect=[email_res, name_res])
        svc = MagicMock()
        svc.send_workspace_ownership_force_transferred = AsyncMock(return_value=True)
        with patch.object(admin_mod, "get_email_service", return_value=svc):
            await admin_mod._notify_force_transfer_best_effort(db, WS, PREV)
        svc.send_workspace_ownership_force_transferred.assert_awaited_once_with(
            to_email="prev@owner.com", workspace_name="My WS"
        )

    @pytest.mark.asyncio
    async def test_skips_when_previous_owner_has_no_email(self):
        db = MagicMock()
        email_res = MagicMock()
        email_res.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=email_res)
        svc = MagicMock()
        svc.send_workspace_ownership_force_transferred = AsyncMock()
        with patch.object(admin_mod, "get_email_service", return_value=svc):
            await admin_mod._notify_force_transfer_best_effort(db, WS, PREV)
        svc.send_workspace_ownership_force_transferred.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_resolution_error(self):
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("db down"))
        with patch.object(admin_mod, "get_email_service") as ges:
            await admin_mod._notify_force_transfer_best_effort(db, WS, PREV)  # no raise
        ges.assert_not_called()


class TestForceTransferRouteServiceErrors:
    """The route maps the service's canonical exceptions to HTTP status at the
    edge (via the app-wide MemoryCloudException handler)."""

    def test_nonexistent_target_returns_400(self):
        from utils.exceptions import BadRequestError

        svc = MagicMock()
        svc.force_transfer_ownership = AsyncMock(
            side_effect=BadRequestError("Target user does not exist", error_code="WS-OWNER-002")
        )
        with patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc):
            resp = _client().post(_PATH, json={"target_user_id": TARGET, "reason": "valid reason"})
        assert resp.status_code == 400

    def test_missing_workspace_returns_404(self):
        from utils.exceptions import NotFoundException

        svc = MagicMock()
        svc.force_transfer_ownership = AsyncMock(side_effect=NotFoundException("Workspace"))
        with patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc):
            resp = _client().post(_PATH, json={"target_user_id": TARGET, "reason": "valid reason"})
        assert resp.status_code == 404

    def test_malformed_workspace_id_returns_422(self):
        resp = _client().post(
            "/admin/workspaces/not-a-uuid/force-transfer-ownership",
            json={"target_user_id": TARGET, "reason": "valid reason"},
        )
        assert resp.status_code == 422
