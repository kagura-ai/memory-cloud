"""System-admin worker app lifecycle API tests (#1315, #1339)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes.worker_apps import (
    WorkerAppCreateRequest,
    WorkerAppRotateSecretRequest,
    create_worker_app,
    rotate_worker_app_secret,
)
from models.worker_app import WorkerAppIdentity
from utils.exceptions import WorkerAppOperationError


def _assert_no_secret_material(mock_logger, secret_values):
    """No log call may carry secret plaintext/ciphertext in event or kwargs."""
    for call in [*mock_logger.info.call_args_list, *mock_logger.error.call_args_list]:
        rendered = (
            " ".join(str(arg) for arg in call.args)
            + " "
            + " ".join(f"{k}={v}" for k, v in call.kwargs.items())
        )
        for secret in secret_values:
            assert secret not in rendered


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


@pytest.mark.asyncio
async def test_create_logs_audit_event_without_secret_material():
    """#1339: create emits a structlog audit event (actor + non-secret fields
    only) after commit — secret plaintext/ciphertext never reaches a log."""
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
    identity.active_signing_secret_encrypted = "ciphertext-material"

    with (
        patch("api.routes.worker_apps.WorkerAppIdentityService") as service_cls,
        patch("api.routes.worker_apps.logger") as mock_logger,
    ):
        service_cls.return_value.create_identity = AsyncMock(return_value=identity)
        await create_worker_app(
            WorkerAppCreateRequest(
                platform="slack",
                app_key="sales",
                display_name="Sales app",
                signing_secret="plaintext-secret",
            ),
            {"user_id": "admin-1"},
            db,
        )

    mock_logger.info.assert_called_once()
    event, kwargs = mock_logger.info.call_args.args[0], mock_logger.info.call_args.kwargs
    assert event == "worker_app_created"
    assert kwargs["requested_by"] == "admin-1"
    assert kwargs["app_key"] == "sales"
    assert kwargs["active_secret_revision"] == 1
    _assert_no_secret_material(mock_logger, ["plaintext-secret", "ciphertext-material"])


@pytest.mark.asyncio
async def test_rotate_logs_audit_event_without_secret_material():
    """#1339: rotate-secret emits an audit event carrying actor, revisions and
    the retiring window — never the secret itself."""
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
        active_secret_revision=5,
        retiring_secret_revision=4,
        config_version=3,
        created_at=datetime(2026, 7, 17),
        updated_at=datetime(2026, 7, 17),
    )

    with (
        patch("api.routes.worker_apps.WorkerAppIdentityService") as service_cls,
        patch("api.routes.worker_apps.logger") as mock_logger,
    ):
        service_cls.return_value.rotate_secret = AsyncMock(return_value=identity)
        await rotate_worker_app_secret(
            WorkerAppRotateSecretRequest(
                signing_secret="rotated-plaintext", retiring_for_seconds=600
            ),
            "slack",
            "sales",
            {"user_id": "admin-2"},
            db,
        )

    mock_logger.info.assert_called_once()
    event, kwargs = mock_logger.info.call_args.args[0], mock_logger.info.call_args.kwargs
    assert event == "worker_app_secret_rotated"
    assert kwargs["requested_by"] == "admin-2"
    assert kwargs["active_secret_revision"] == 5
    assert kwargs["retiring_secret_revision"] == 4
    assert kwargs["retiring_for_seconds"] == 600
    _assert_no_secret_material(mock_logger, ["rotated-plaintext"])


@pytest.mark.asyncio
async def test_rotate_failure_logs_cause_with_exc_info():
    """#1339: an unexpected failure is mapped to the fixed-message
    WorkerAppOperationError for the client, but the server log captures the
    underlying cause via exc_info (with actor/app context, no secret)."""
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    with (
        patch("api.routes.worker_apps.WorkerAppIdentityService") as service_cls,
        patch("api.routes.worker_apps.logger") as mock_logger,
    ):
        service_cls.return_value.rotate_secret = AsyncMock(
            side_effect=RuntimeError("encryption backend unavailable")
        )
        with pytest.raises(WorkerAppOperationError):
            await rotate_worker_app_secret(
                WorkerAppRotateSecretRequest(signing_secret="rotated-plaintext"),
                "slack",
                "sales",
                {"user_id": "admin-2"},
                db,
            )

    mock_logger.error.assert_called_once()
    event, kwargs = mock_logger.error.call_args.args[0], mock_logger.error.call_args.kwargs
    assert event == "worker_app_rotate_failed"
    assert kwargs["requested_by"] == "admin-2"
    assert kwargs["app_key"] == "sales"
    assert kwargs["exc_info"] is True
    _assert_no_secret_material(mock_logger, ["rotated-plaintext"])
    db.rollback.assert_awaited_once()
