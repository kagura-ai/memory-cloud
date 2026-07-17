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


# ---------------------------------------------------------------------------
# #1339: audit logging + failure diagnostics for the secret lifecycle.
# ---------------------------------------------------------------------------

SECRET = "plaintext-secret"  # noqa: S105 — test fixture value
CIPHERTEXT = "ciphertext"


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _identity(**overrides) -> WorkerAppIdentity:
    identity = WorkerAppIdentity(
        id=uuid4(),
        platform="slack",
        app_key="sales",
        display_name="Sales app",
        status="active",
        active_secret_revision=2,
        retiring_secret_revision=1,
        config_version=3,
        created_at=datetime(2026, 7, 17),
        updated_at=datetime(2026, 7, 17),
    )
    identity.active_signing_secret_encrypted = CIPHERTEXT
    for key, value in overrides.items():
        setattr(identity, key, value)
    return identity


def _assert_no_secret_material(mock_logger):
    """AC: no secret material or ciphertext in ANY log call."""
    for call in mock_logger.mock_calls:
        rendered = repr(call)
        assert SECRET not in rendered
        assert CIPHERTEXT not in rendered


async def _create(db):
    from api.routes.worker_apps import create_worker_app

    return await create_worker_app(
        WorkerAppCreateRequest(
            platform="slack",
            app_key="sales",
            display_name="Sales app",
            signing_secret=SECRET,
        ),
        {"user_id": "admin-1"},
        db,
    )


async def _update(db):
    from api.routes.worker_apps import WorkerAppUpdateRequest, update_worker_app

    return await update_worker_app(
        WorkerAppUpdateRequest(status="disabled"),
        "slack",
        "sales",
        {"user_id": "admin-1"},
        db,
    )


async def _rotate(db):
    from api.routes.worker_apps import (
        WorkerAppRotateSecretRequest,
        rotate_worker_app_secret,
    )

    return await rotate_worker_app_secret(
        WorkerAppRotateSecretRequest(signing_secret=SECRET, retiring_for_seconds=1800),
        "slack",
        "sales",
        {"user_id": "admin-1"},
        db,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "service_method", "event"),
    [
        (_create, "create_identity", "worker_app_created"),
        (_update, "update_identity", "worker_app_updated"),
        (_rotate, "rotate_secret", "worker_app_secret_rotated"),
    ],
)
async def test_lifecycle_success_emits_audit_event_without_secret(call, service_method, event):
    db = _db()
    with (
        patch("api.routes.worker_apps.WorkerAppIdentityService") as service_cls,
        patch("api.routes.worker_apps.logger") as mock_logger,
    ):
        setattr(service_cls.return_value, service_method, AsyncMock(return_value=_identity()))
        await call(db)

    events = [c for c in mock_logger.info.call_args_list if c.args and c.args[0] == event]
    assert len(events) == 1, f"expected one '{event}' success event"
    fields = events[0].kwargs
    assert fields["actor"] == "admin-1"
    assert fields["platform"] == "slack"
    assert fields["app_key"] == "sales"
    _assert_no_secret_material(mock_logger)


@pytest.mark.asyncio
async def test_update_success_event_carries_revision_context():
    """A status flip (enable/disable) must be correlatable with the secret
    material active/retiring at that moment (Copilot on PR #1340/#1342)."""
    db = _db()
    with (
        patch("api.routes.worker_apps.WorkerAppIdentityService") as service_cls,
        patch("api.routes.worker_apps.logger") as mock_logger,
    ):
        service_cls.return_value.update_identity = AsyncMock(
            return_value=_identity(status="disabled")
        )
        await _update(db)

    fields = mock_logger.info.call_args.kwargs
    assert fields["status"] == "disabled"
    assert fields["active_secret_revision"] == 2
    assert fields["retiring_secret_revision"] == 1
    assert "retiring_valid_until" in fields
    _assert_no_secret_material(mock_logger)


@pytest.mark.asyncio
async def test_rotate_success_event_carries_revisions_and_window():
    db = _db()
    with (
        patch("api.routes.worker_apps.WorkerAppIdentityService") as service_cls,
        patch("api.routes.worker_apps.logger") as mock_logger,
    ):
        service_cls.return_value.rotate_secret = AsyncMock(return_value=_identity())
        await _rotate(db)

    fields = mock_logger.info.call_args.kwargs
    assert fields["active_secret_revision"] == 2
    assert fields["retiring_secret_revision"] == 1
    assert fields["retiring_for_seconds"] == 1800
    _assert_no_secret_material(mock_logger)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "service_method", "operation"),
    [
        (_create, "create_identity", "create"),
        (_update, "update_identity", "update"),
        (_rotate, "rotate_secret", "rotate"),
    ],
)
async def test_unexpected_failure_logs_cause_with_exc_info(call, service_method, operation):
    from utils.exceptions import WorkerAppOperationError

    db = _db()
    with (
        patch("api.routes.worker_apps.WorkerAppIdentityService") as service_cls,
        patch("api.routes.worker_apps.logger") as mock_logger,
    ):
        setattr(
            service_cls.return_value,
            service_method,
            AsyncMock(side_effect=RuntimeError("db exploded")),
        )
        with pytest.raises(WorkerAppOperationError):
            await call(db)

    assert mock_logger.error.call_count == 1
    kwargs = mock_logger.error.call_args.kwargs
    assert kwargs.get("exc_info") is True
    assert kwargs["actor"] == "admin-1"
    assert kwargs["platform"] == "slack"
    assert kwargs["app_key"] == "sales"
    assert kwargs["operation"] == operation
    _assert_no_secret_material(mock_logger)


@pytest.mark.asyncio
async def test_expected_domain_errors_do_not_log_exc_info():
    # ConflictError / MemoryCloudException arms re-raise untouched — the
    # exc_info diagnostics are for the UNEXPECTED arm only.
    from utils.exceptions import ConflictError

    db = _db()
    with (
        patch("api.routes.worker_apps.WorkerAppIdentityService") as service_cls,
        patch("api.routes.worker_apps.logger") as mock_logger,
    ):
        service_cls.return_value.create_identity = AsyncMock(
            side_effect=ConflictError("already exists")
        )
        with pytest.raises(ConflictError):
            await _create(db)

    mock_logger.error.assert_not_called()
