"""Tests for ``storage.factory`` (Issue #485).

The factory is a tiny indirection layer, but it is also the only place
the dispatch from "is R2 configured?" → concrete impl lives. Bugs here
surface as cryptic 500s in the upload path, so the contract is worth
pinning down explicitly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from storage import factory
from storage.r2 import R2Storage


@pytest.fixture(autouse=True)
def _reset_factory():
    """Drop the cached singleton between tests.

    Without this, the first test seeds ``factory._storage`` and every
    subsequent test sees the wrong instance. ``_reset_for_tests`` is
    declared in factory.py exactly for this purpose.
    """
    factory._reset_for_tests()
    yield
    factory._reset_for_tests()


def _settings_with_r2(**overrides):
    """Build a Mock ``Settings`` with the S3-compatible storage fields populated.

    (#994 renamed r2_* -> storage_*; the discriminator must be a real string so
    the factory's `.strip().lower()` allowlist check does not see a MagicMock.)
    """
    from unittest.mock import MagicMock

    s = MagicMock()
    s.storage_backend_type = "r2"
    s.storage_region = "auto"
    s.storage_account_id = "acct"
    s.storage_access_key_id = "key"
    s.storage_secret_access_key = "secret"
    s.storage_bucket = "kagura-files-test"
    s.storage_endpoint_url = "https://acct.r2.cloudflarestorage.com"
    # Explicit bool — bare MagicMock attribute access would return a truthy
    # child and silently flip the ChecksumSHA256 binding on (#556).
    s.storage_checksum_binding_enabled = False
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _settings_without_r2():
    return _settings_with_r2(storage_endpoint_url="")


class TestGetBlobStorage:
    def test_raises_when_r2_endpoint_url_missing(self):
        """``get_blob_storage`` MUST raise ``ExternalServiceError``
        (HTTP 502) — not RuntimeError — when R2 is not configured, so
        REST and MCP file handlers map cleanly to 502/service_unavailable
        instead of an opaque 500 (Copilot loop 3 fix on PR #551)."""
        from utils.exceptions import ExternalServiceError

        with patch("storage.factory.get_settings", return_value=_settings_without_r2()):
            with pytest.raises(ExternalServiceError, match="not configured"):
                factory.get_blob_storage()

    def test_returns_r2_storage_when_configured(self):
        with patch("storage.factory.get_settings", return_value=_settings_with_r2()):
            storage = factory.get_blob_storage()
            assert isinstance(storage, R2Storage)

    def test_returns_same_instance_on_repeat_calls(self):
        """Singleton: the second call returns the cached instance."""
        with patch("storage.factory.get_settings", return_value=_settings_with_r2()):
            first = factory.get_blob_storage()
            second = factory.get_blob_storage()
            assert first is second

    def test_checksum_binding_flag_wires_through_to_r2_storage(self):
        """Issue #556: the factory MUST forward
        ``settings.r2_checksum_binding_enabled`` into the constructed
        ``R2Storage`` instance. Without this wiring, the entire feature
        gate is silently dead even after operators flip the env var."""
        with patch(
            "storage.factory.get_settings",
            return_value=_settings_with_r2(storage_checksum_binding_enabled=True),
        ):
            storage = factory.get_blob_storage()
            assert isinstance(storage, R2Storage)
            assert storage._enable_checksum_binding is True

    def test_checksum_binding_flag_default_off_wires_through(self):
        """Default-off path is the deploy-day behavior; pin it explicitly
        so a future refactor that accidentally hardcodes ``True`` here
        would fail this test instead of silently breaking the staged
        rollout."""
        with patch("storage.factory.get_settings", return_value=_settings_with_r2()):
            storage = factory.get_blob_storage()
            assert isinstance(storage, R2Storage)
            assert storage._enable_checksum_binding is False


class TestCloseBlobStorage:
    @pytest.mark.asyncio
    async def test_close_when_uninitialized_is_noop(self):
        """``close_blob_storage`` is safe to call when storage was never
        constructed (lifespan shutdown on a server with R2 disabled)."""
        await factory.close_blob_storage()  # must not raise
        assert factory._storage is None

    @pytest.mark.asyncio
    async def test_close_releases_cached_instance(self):
        """After close, the cached instance is dropped — a subsequent
        ``get_blob_storage`` call constructs a fresh one."""
        with patch("storage.factory.get_settings", return_value=_settings_with_r2()):
            first = factory.get_blob_storage()
            await factory.close_blob_storage()
            assert factory._storage is None
            second = factory.get_blob_storage()
            assert first is not second
