"""Route tests for the break-glass admin force-transfer endpoint (#1101).

DB-free: dependency overrides + a patched service. Verifies the system-admin gate,
the required non-empty reason, the path-bound workspace id, the response shape, and
that the displaced PREVIOUS owner is the notification target.
"""

from __future__ import annotations

from datetime import datetime
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


def _settings(dual: bool) -> MagicMock:
    s = MagicMock()
    s.require_dual_control_force_transfer = dual
    return s


def _pending_req(rid):
    req = MagicMock()
    req.id = rid
    req.workspace_id = WS
    req.target_user_id = TARGET
    req.initiated_by_user_id = ADMIN
    req.status = "pending"
    return req


class TestDualControlInitiate:
    """#1113: with dual-control enabled, the POST files a pending request (202)
    instead of transferring immediately."""

    def test_enabled_files_pending_request_202(self):
        rid = uuid4()
        svc = MagicMock()
        svc.initiate_force_transfer = AsyncMock(return_value=_pending_req(rid))
        svc.force_transfer_ownership = AsyncMock()
        notify = AsyncMock()
        with (
            patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc),
            patch.object(admin_mod, "get_settings", return_value=_settings(True)),
            patch.object(admin_mod, "_notify_force_transfer_best_effort", notify),
        ):
            resp = _client().post(
                _PATH, json={"target_user_id": TARGET, "reason": "owner unreachable"}
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "pending_approval"
        assert body["request_id"] == str(rid)
        assert body["dual_control"] is True
        # The immediate transfer must NOT run; no notification on a mere filing.
        svc.force_transfer_ownership.assert_not_awaited()
        svc.initiate_force_transfer.assert_awaited_once()
        notify.assert_not_awaited()

    def test_disabled_transfers_immediately_200(self):
        svc = MagicMock()
        svc.force_transfer_ownership = AsyncMock(return_value=_result())
        svc.initiate_force_transfer = AsyncMock()
        with (
            patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc),
            patch.object(admin_mod, "get_settings", return_value=_settings(False)),
            patch.object(admin_mod, "_notify_force_transfer_best_effort", AsyncMock()),
        ):
            resp = _client().post(_PATH, json={"target_user_id": TARGET, "reason": "unreachable"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["dual_control"] is False
        svc.initiate_force_transfer.assert_not_awaited()
        svc.force_transfer_ownership.assert_awaited_once()


class TestDualControlApprove:
    _APPROVE = staticmethod(lambda rid: f"/admin/force-transfer-requests/{rid}/approve")

    def test_approve_transfers_and_notifies_200(self):
        rid = uuid4()
        svc = MagicMock()
        svc.approve_force_transfer = AsyncMock(return_value=_result())
        notify = AsyncMock()
        with (
            patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc),
            patch.object(admin_mod, "_notify_force_transfer_best_effort", notify),
        ):
            resp = _client().post(self._APPROVE(rid))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "approved"
        assert body["new_owner_id"] == TARGET
        call = svc.approve_force_transfer.await_args.kwargs
        assert call["request_id"] == rid
        assert call["approver_user_id"] == ADMIN
        notify.assert_awaited_once()
        assert notify.await_args.args[2] == PREV

    def test_self_approval_maps_to_400(self):
        from utils.exceptions import BadRequestError

        svc = MagicMock()
        svc.approve_force_transfer = AsyncMock(
            side_effect=BadRequestError(
                "A force-transfer must be approved by a different system admin than the "
                "one who initiated it",
                error_code="WS-OWNER-DUAL-001",
            )
        )
        with patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc):
            resp = _client().post(self._APPROVE(uuid4()))
        assert resp.status_code == 400

    def test_stale_request_maps_to_409(self):
        from utils.exceptions import ConflictError

        svc = MagicMock()
        svc.approve_force_transfer = AsyncMock(
            side_effect=ConflictError("Workspace ownership changed since this force-transfer")
        )
        with patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc):
            resp = _client().post(self._APPROVE(uuid4()))
        assert resp.status_code == 409

    def test_unknown_request_maps_to_404(self):
        from utils.exceptions import NotFoundException

        svc = MagicMock()
        svc.approve_force_transfer = AsyncMock(
            side_effect=NotFoundException("Force-transfer request")
        )
        with patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc):
            resp = _client().post(self._APPROVE(uuid4()))
        assert resp.status_code == 404

    def test_malformed_request_id_is_422(self):
        resp = _client().post("/admin/force-transfer-requests/not-a-uuid/approve")
        assert resp.status_code == 422

    def test_non_admin_is_403(self):
        resp = _client(role="user").post(self._APPROVE(uuid4()))
        assert resp.status_code == 403


class TestDualControlCancel:
    @staticmethod
    def _cancel(rid) -> str:
        return f"/admin/force-transfer-requests/{rid}/cancel"

    def test_cancel_pending_200(self):
        rid = uuid4()
        cancelled = _pending_req(rid)
        cancelled.status = "cancelled"
        svc = MagicMock()
        svc.cancel_force_transfer = AsyncMock(return_value=cancelled)
        with patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc):
            resp = _client().post(self._cancel(rid))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelled"
        assert svc.cancel_force_transfer.await_args.kwargs["request_id"] == rid

    def test_cancel_non_pending_maps_to_409(self):
        from utils.exceptions import ConflictError

        svc = MagicMock()
        svc.cancel_force_transfer = AsyncMock(
            side_effect=ConflictError("Force-transfer request is not pending (status=approved)")
        )
        with patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc):
            resp = _client().post(self._cancel(uuid4()))
        assert resp.status_code == 409

    def test_non_admin_is_403(self):
        resp = _client(role="user").post(self._cancel(uuid4()))
        assert resp.status_code == 403


class TestDualControlGet:
    @staticmethod
    def _get(rid) -> str:
        return f"/admin/force-transfer-requests/{rid}"

    def test_get_returns_request_details_200(self):
        rid = uuid4()
        req = _pending_req(rid)
        req.reason = "owner unreachable"
        req.initiated_by_email = "ada@test.com"
        req.ownership_epoch_at_initiation = 3
        req.created_at = datetime(2026, 6, 28, 12, 0, 0)
        req.decided_by_user_id = None
        req.decided_at = None
        svc = MagicMock()
        svc.get_force_transfer_request = AsyncMock(return_value=req)
        with patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc):
            resp = _client().get(self._get(rid))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["request_id"] == str(rid)
        assert body["target_user_id"] == TARGET
        assert body["reason"] == "owner unreachable"
        assert body["status"] == "pending"
        assert body["decided_at"] is None
        assert svc.get_force_transfer_request.await_args.kwargs["request_id"] == rid

    def test_get_unknown_request_404(self):
        from utils.exceptions import NotFoundException

        svc = MagicMock()
        svc.get_force_transfer_request = AsyncMock(
            side_effect=NotFoundException("Force-transfer request")
        )
        with patch.object(admin_mod, "WorkspaceOwnershipService", return_value=svc):
            resp = _client().get(self._get(uuid4()))
        assert resp.status_code == 404

    def test_get_malformed_request_id_is_422(self):
        resp = _client().get("/admin/force-transfer-requests/not-a-uuid")
        assert resp.status_code == 422

    def test_get_non_admin_is_403(self):
        resp = _client(role="user").get(self._get(uuid4()))
        assert resp.status_code == 403
