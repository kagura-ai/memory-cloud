"""Unit coverage for worker app signing-secret custody (#1315)."""

import pytest
from cryptography.fernet import Fernet

from models.worker_app import WorkerAppIdentity


@pytest.fixture
def _fernet_env(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET", Fernet.generate_key().decode())
    import utils.encryption as enc_module

    enc_module._encryptor = None
    yield
    enc_module._encryptor = None


def test_signing_secrets_round_trip_without_plaintext_at_rest(_fernet_env):
    identity = WorkerAppIdentity()
    identity.set_active_signing_secret("active-secret")
    identity.set_retiring_signing_secret("retiring-secret")

    assert "active-secret" not in identity.active_signing_secret_encrypted
    assert "retiring-secret" not in identity.retiring_signing_secret_encrypted
    assert identity.get_active_signing_secret() == "active-secret"
    assert identity.get_retiring_signing_secret() == "retiring-secret"


def test_app_identity_has_global_platform_key_uniqueness():
    unique_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in WorkerAppIdentity.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("platform", "app_key") in unique_sets
