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
    """Build a Mock ``Settings`` object with R2 fields populated."""
    from unittest.mock import MagicMock

    s = MagicMock()
    s.r2_account_id = "acct"
    s.r2_access_key_id = "key"
    s.r2_secret_access_key = "secret"
    s.r2_bucket = "kagura-files-test"
    s.r2_endpoint_url = "https://acct.r2.cloudflarestorage.com"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _settings_without_r2():
    return _settings_with_r2(r2_endpoint_url="")


class TestGetBlobStorage:
    def test_raises_when_r2_endpoint_url_missing(self):
        """``get_blob_storage`` MUST raise (not silently no-op) when R2
        is not configured — callers should gate on
        ``settings.r2_endpoint_url`` upstream."""
        with patch("storage.factory.get_settings", return_value=_settings_without_r2()):
            with pytest.raises(RuntimeError, match="R2 storage is not configured"):
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
