"""System-admin worker app lifecycle API tests (#1315)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes.worker_apps import WorkerAppCreateRequest, create_worker_app
from models.worker_app import WorkerAppIdentity


@pytest.mark.asyncio
async def test_admin_create_response_never_exposes_signing_secret():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    identity = WorkerAppIdentity(
        id=uuid4(),
        platform="slack",
        app_key="sales",
        display_name="Sales app",
        status="active",
        active_secret_revision=1,
        config_version=1,
        created_at=datetime(2026, 7, 17),
        updated_at=datetime(2026, 7, 17),
    )
    identity.active_signing_secret_encrypted = "ciphertext"

    with patch("api.routes.worker_apps.WorkerAppIdentityService") as service_cls:
        service_cls.return_value.create_identity = AsyncMock(return_value=identity)
        response = await create_worker_app(
            WorkerAppCreateRequest(
                platform="slack",
                app_key="sales",
                display_name="Sales app",
                signing_secret="plaintext-secret",
            ),
            {"user_id": "admin-1"},
            db,
        )

    dumped = response.model_dump()
    assert dumped["has_active_secret"] is True
    assert "signing_secret" not in dumped
    assert "active_signing_secret_encrypted" not in dumped
    service_cls.return_value.create_identity.assert_awaited_once_with(
        platform="slack",
        app_key="sales",
        display_name="Sales app",
        signing_secret="plaintext-secret",
        actor_id="admin-1",
    )
