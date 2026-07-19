"""HTTP-level admin gate pin for /admin/worker-apps (#1360 item 2).

The existing tests in ``test_worker_apps.py`` call the route handlers
directly, which bypasses FastAPI dependency injection — they prove the
handler logic but not that ``require_admin`` actually guards the HTTP
surface. This module exercises the real DI chain (real ``require_admin``,
only ``get_current_user`` overridden) through a ``TestClient``, mirroring
``test_admin_force_transfer_route.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.routes import worker_apps as worker_apps_mod
from auth.dependencies import get_current_user
from db.base import get_db
from utils.exceptions import MemoryCloudException


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(worker_apps_mod.router)

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
        return {"user_id": "admin_user_1", "email": "admin@test.com", "role": role}

    async def _db():
        yield MagicMock()

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


_NON_ADMIN_CALLS = [
    ("GET", "/admin/worker-apps", None),
    (
        "POST",
        "/admin/worker-apps",
        {
            "platform": "slack",
            "app_key": "sales",
            "display_name": "Sales",
            "signing_secret": "s3cret",
        },
    ),
    ("PATCH", "/admin/worker-apps/slack/sales", {"display_name": "Renamed"}),
    (
        "POST",
        "/admin/worker-apps/slack/sales/rotate-secret",
        {"signing_secret": "n3w", "retiring_for_seconds": 0},
    ),
]


class TestWorkerAppsHttpGate:
    @pytest.mark.parametrize(("method", "path", "body"), _NON_ADMIN_CALLS)
    def test_non_admin_is_403_via_http(self, method, path, body):
        resp = _client(role="user").request(method, path, json=body)
        assert resp.status_code == 403

    def test_admin_passes_gate_via_http(self):
        """The gate admits role=admin — proves the 403s above come from
        require_admin, not from a broken route wiring."""
        svc = MagicMock()
        svc.list_identities = AsyncMock(return_value=[])
        with patch.object(worker_apps_mod, "WorkerAppIdentityService", return_value=svc):
            resp = _client(role="admin").get("/admin/worker-apps")
        assert resp.status_code == 200
        assert resp.json() == []
