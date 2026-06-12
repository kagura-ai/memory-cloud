"""Issue #994 — storage settings env-var backward compatibility.

The five R2 storage settings were renamed `r2_*` -> `storage_*` with
`STORAGE_*` / `S3_*` / `R2_*` accepted as aliases. Because `Settings` uses
`extra="ignore"`, a rename WITHOUT the `r2_*` alias would make a prod deploy's
existing `R2_*` env vars silently dropped -> empty endpoint -> the upload path
502s on first use with no load-time error. These tests pin the acceptance
criterion "existing prod R2 deploy works unchanged with current env vars".
"""

from __future__ import annotations

import pytest

from config.settings import Settings

# All env keys the storage fields can resolve from, cleared before each case so
# the host's real .env / shell does not leak into the assertion.
_STORAGE_ENV_KEYS = [
    "STORAGE_BACKEND_TYPE",
    "STORAGE_ACCOUNT_ID",
    "S3_ACCOUNT_ID",
    "R2_ACCOUNT_ID",
    "STORAGE_ACCESS_KEY_ID",
    "S3_ACCESS_KEY_ID",
    "R2_ACCESS_KEY_ID",
    "STORAGE_SECRET_ACCESS_KEY",
    "S3_SECRET_ACCESS_KEY",
    "R2_SECRET_ACCESS_KEY",
    "STORAGE_BUCKET",
    "S3_BUCKET",
    "R2_BUCKET",
    "STORAGE_ENDPOINT_URL",
    "S3_ENDPOINT_URL",
    "R2_ENDPOINT_URL",
    "STORAGE_CHECKSUM_BINDING_ENABLED",
    "S3_CHECKSUM_BINDING_ENABLED",
    "R2_CHECKSUM_BINDING_ENABLED",
]


@pytest.fixture
def clean_storage_env(monkeypatch):
    for k in _STORAGE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _fresh_settings() -> Settings:
    # _env_file=None ignores .env.dev so only os.environ (monkeypatched) is read.
    return Settings(_env_file=None)


def test_legacy_r2_env_vars_still_resolve_storage_fields(clean_storage_env):
    """Prod sets only R2_* and no STORAGE_*; the fields MUST still populate."""
    clean_storage_env.setenv("R2_ACCOUNT_ID", "acct")
    clean_storage_env.setenv("R2_ACCESS_KEY_ID", "ak")
    clean_storage_env.setenv("R2_SECRET_ACCESS_KEY", "sk")
    clean_storage_env.setenv("R2_BUCKET", "kagura-memory-files-prod")
    clean_storage_env.setenv(
        "R2_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com"
    )
    clean_storage_env.setenv("R2_CHECKSUM_BINDING_ENABLED", "true")

    s = _fresh_settings()

    assert s.storage_account_id == "acct"
    assert s.storage_access_key_id == "ak"
    assert s.storage_secret_access_key == "sk"
    assert s.storage_bucket == "kagura-memory-files-prod"
    assert s.storage_endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert s.storage_checksum_binding_enabled is True


def test_canonical_storage_env_vars_resolve(clean_storage_env):
    clean_storage_env.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
    clean_storage_env.setenv("STORAGE_BUCKET", "kagura-files")
    clean_storage_env.setenv("STORAGE_ACCESS_KEY_ID", "minioadmin")
    clean_storage_env.setenv("STORAGE_SECRET_ACCESS_KEY", "minioadmin")
    clean_storage_env.setenv("STORAGE_BACKEND_TYPE", "minio")

    s = _fresh_settings()

    assert s.storage_endpoint_url == "http://minio:9000"
    assert s.storage_bucket == "kagura-files"
    assert s.storage_backend_type == "minio"


def test_canonical_storage_wins_over_legacy_r2_when_both_set(clean_storage_env):
    """AliasChoices order is (storage_, s3_, r2_) — first match wins."""
    clean_storage_env.setenv("R2_ENDPOINT_URL", "https://legacy.example.com")
    clean_storage_env.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")

    s = _fresh_settings()

    assert s.storage_endpoint_url == "http://minio:9000"


def test_storage_backend_type_defaults_to_r2(clean_storage_env):
    """Prod sets no discriminator; default MUST be the zero-change 'r2'."""
    s = _fresh_settings()
    assert s.storage_backend_type == "r2"
