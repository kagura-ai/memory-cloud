"""Lifecycle tests for worker app secret rotation (#1315)."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from models.worker_app import WorkerAppIdentity
from services.worker_app_identity import WorkerAppIdentityService, identity_collection_revision
from utils.datetime import utcnow


@pytest.fixture
def _fernet_env(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET", Fernet.generate_key().decode())
    import utils.encryption as enc_module

    enc_module._encryptor = None
    yield
    enc_module._encryptor = None


@pytest.mark.asyncio
async def test_rotate_keeps_previous_secret_for_bounded_window(_fernet_env):
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=4,
        config_version=8,
    )
    identity.set_active_signing_secret("old-secret")
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    before = utcnow()
    result = await service.rotate_secret(
        platform="slack",
        app_key="sales",
        signing_secret="new-secret",
        retiring_for_seconds=3600,
        actor_id="admin-1",
    )

    assert result.get_active_signing_secret() == "new-secret"
    assert result.active_secret_revision == 5
    assert result.get_retiring_signing_secret() == "old-secret"
    assert result.retiring_secret_revision == 4
    assert before + timedelta(seconds=3599) <= result.retiring_valid_until
    assert result.config_version == 9
    assert result.status == "active"


@pytest.mark.asyncio
async def test_disable_preserves_ciphertext_but_bumps_revision(_fernet_env):
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=1,
        config_version=1,
    )
    identity.set_active_signing_secret("secret")
    ciphertext = identity.active_signing_secret_encrypted
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        status="disabled",
    )

    assert result.status == "disabled"
    assert result.config_version == 2
    assert result.active_signing_secret_encrypted == ciphertext


@pytest.mark.asyncio
async def test_rotate_disabled_identity_stays_disabled_and_drops_revoked_secret(_fernet_env):
    """Revocation is sticky: rotating a disabled identity stages the new
    secret but must NOT resurrect the identity to active, and must NOT arm
    the (revoked) previous secret as a retiring revision that /workers/apps
    would re-serve to the fleet after a later re-enable."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="disabled",
        active_secret_revision=3,
        config_version=5,
    )
    identity.set_active_signing_secret("compromised")
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.rotate_secret(
        platform="slack",
        app_key="sales",
        signing_secret="fresh-secret",
        retiring_for_seconds=3600,
        actor_id="admin-1",
    )

    assert result.status == "disabled"
    assert result.get_active_signing_secret() == "fresh-secret"
    assert result.active_secret_revision == 4
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_secret_revision is None
    assert result.retiring_valid_until is None
    assert result.config_version == 6


@pytest.mark.asyncio
async def test_rotate_unconfigured_identity_preserves_status(_fernet_env):
    """Staging a secret into the migration-window 'unconfigured' default
    must not flip it to active — that would switch config dispatch from the
    worker-env path to identity-governed before the operator opts in
    (enable stays the explicit update_identity(status='active') step)."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="default",
        display_name="Default",
        status="unconfigured",
        config_version=1,
    )
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.rotate_secret(
        platform="slack",
        app_key="default",
        signing_secret="staged-secret",
        retiring_for_seconds=3600,
        actor_id="admin-1",
    )

    assert result.status == "unconfigured"
    assert result.get_active_signing_secret() == "staged-secret"
    assert result.active_secret_revision == 1
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_valid_until is None


@pytest.mark.asyncio
async def test_rotate_recovers_when_previous_ciphertext_undecryptable(_fernet_env):
    """#1356: rotate is the documented recovery path after a Fernet key
    rotation or ciphertext corruption — it must not 500 on the stored
    ciphertext. The unrecoverable previous secret is dropped (no retiring
    window) and the new secret is stored; that write IS the recovery."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=4,
        config_version=8,
    )
    # Stored ciphertext that the current API_KEY_SECRET cannot decrypt.
    identity.active_signing_secret_encrypted = "gAAAAA-not-decryptable-with-current-key"
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    with patch("services.worker_app_identity.logger") as mock_logger:
        result = await service.rotate_secret(
            platform="slack",
            app_key="sales",
            signing_secret="new-secret",
            retiring_for_seconds=3600,
            actor_id="admin-1",
        )

    assert result.get_active_signing_secret() == "new-secret"
    assert result.active_secret_revision == 5
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_secret_revision is None
    assert result.retiring_valid_until is None
    assert result.config_version == 9
    assert result.status == "active"
    # Audit: the drop is recorded, ids/enums only — never secret material.
    assert mock_logger.warning.call_count == 1
    kwargs = mock_logger.warning.call_args.kwargs
    assert mock_logger.warning.call_args.args[0] == "worker_app_previous_secret_undecryptable"
    assert kwargs["platform"] == "slack"
    assert kwargs["app_key"] == "sales"
    for value in kwargs.values():
        assert "new-secret" not in str(value)
        assert "gAAAAA" not in str(value)


@pytest.mark.asyncio
async def test_disable_clears_retiring_window(_fernet_env):
    """#1356: disabling revokes ALL secret material acceptance — the armed
    retiring secret must not survive to be revived by a later re-enable."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=5,
        config_version=9,
    )
    identity.set_active_signing_secret("current")
    identity.set_retiring_signing_secret("previous")
    identity.retiring_secret_revision = 4
    identity.retiring_valid_until = utcnow() + timedelta(hours=1)
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        status="disabled",
    )

    assert result.status == "disabled"
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_secret_revision is None
    assert result.retiring_valid_until is None


@pytest.mark.asyncio
async def test_reenable_clears_stale_retiring_window(_fernet_env):
    """#1356: rows disabled before this fix may still carry retiring
    material — the enable transition must purge it so the old secret does
    not resurface in the worker bootstrap."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="disabled",
        active_secret_revision=5,
        config_version=9,
    )
    identity.set_active_signing_secret("current")
    identity.set_retiring_signing_secret("revoked-old")
    identity.retiring_secret_revision = 4
    identity.retiring_valid_until = utcnow() + timedelta(hours=1)
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        status="active",
    )

    assert result.status == "active"
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_secret_revision is None
    assert result.retiring_valid_until is None


@pytest.mark.asyncio
async def test_status_noop_update_keeps_retiring_window(_fernet_env):
    """A same-status PATCH (e.g. display_name edit sent with status=active)
    is not a transition — it must not tear down a legitimately armed
    rotation window mid-migration."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=5,
        config_version=9,
    )
    identity.set_active_signing_secret("current")
    identity.set_retiring_signing_secret("previous")
    identity.retiring_secret_revision = 4
    valid_until = utcnow() + timedelta(hours=1)
    identity.retiring_valid_until = valid_until
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        status="active",
        display_name="Sales EMEA",
    )

    assert result.status == "active"
    assert result.display_name == "Sales EMEA"
    assert result.get_retiring_signing_secret() == "previous"
    assert result.retiring_secret_revision == 4
    assert result.retiring_valid_until == valid_until


def test_bootstrap_revision_changes_when_retiring_window_expires():
    identity = WorkerAppIdentity(
        id=uuid4(),
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        config_version=2,
        retiring_valid_until=utcnow() + timedelta(minutes=5),
    )
    before_expiry = identity_collection_revision([identity])
    identity.retiring_valid_until = utcnow() - timedelta(seconds=1)

    assert identity_collection_revision([identity]) != before_expiry
