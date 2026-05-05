"""Tests for ``R2Storage`` construction (Issue #485).

The class is a thin wrapper over ``aioboto3.Session`` — actual R2
interactions are deferred to integration tests against a real bucket.
This file pins the construction contract: required-field validation
and Protocol conformance.
"""

from __future__ import annotations

import pytest

from storage.protocol import BlobStorageProtocol
from storage.r2 import R2Storage


def _kwargs(**overrides):
    base = {
        "account_id": "acct",
        "access_key_id": "key",
        "secret_access_key": "secret",
        "bucket": "kagura-files-test",
        "endpoint_url": "https://acct.r2.cloudflarestorage.com",
    }
    base.update(overrides)
    return base


class TestR2StorageConstruction:
    def test_construction_succeeds_with_valid_args(self):
        storage = R2Storage(**_kwargs())
        assert isinstance(storage, BlobStorageProtocol)

    @pytest.mark.parametrize(
        "missing_field",
        [
            "account_id",
            "access_key_id",
            "secret_access_key",
            "bucket",
            "endpoint_url",
        ],
    )
    def test_empty_required_field_raises(self, missing_field):
        """Empty string for any of the 5 required fields → ValueError.

        Catches the half-configured-prod scenario where one env var is
        missing and the deployment silently boots with broken uploads.
        """
        with pytest.raises(ValueError, match="non-empty"):
            R2Storage(**_kwargs(**{missing_field: ""}))

    @pytest.mark.asyncio
    async def test_close_is_noop(self):
        """``close`` is callable on a fresh instance and returns None."""
        storage = R2Storage(**_kwargs())
        assert await storage.close() is None
