"""Lifecycle tests for worker app secret rotation (#1315)."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
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
