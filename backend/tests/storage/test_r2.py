"""Tests for ``R2Storage`` construction (Issue #485) and the
``ChecksumSHA256`` body-binding wiring (Issue #575, #556 follow-up).

The class is a thin wrapper over ``aioboto3.Session`` — actual R2
interactions are deferred to integration tests against a real bucket.
This file pins the construction contract: required-field validation
and Protocol conformance, plus the Python-side ``Params`` shape for
``generate_presigned_put`` (live wire contract lives in
``backend/tests/integration/test_r2_live.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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


# Hardcoded sha256 → base64 pair for ``b""``. The expected base64 is
# pinned as a literal so the test cannot drift in lockstep with
# production: re-deriving via ``base64.b64encode(bytes.fromhex(...))``
# (the same expression production uses) would be tautological — both
# could break together and the assertion would still pass.
_SHA256_HEX_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_SHA256_B64_EMPTY = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="

_SIGNED_URL = "https://r2.example/signed"
_PUT_KWARGS = {
    "key": "objects/abc.bin",
    "content_type": "application/octet-stream",
    "size_bytes": 1234,
    "ttl_seconds": 300,
    "sha256": _SHA256_HEX_EMPTY,
}


class _FakeAsyncClientCM:
    """Minimal async context manager that yields a pre-built fake client.

    Mirrors the shape of ``aioboto3.Session.client(...)`` so that
    ``async with storage._client() as client`` yields the same mock
    the test holds a reference to.
    """

    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, *_: object) -> bool:
        return False


def _make_storage_with_fake_client(
    *, enable_checksum_binding: bool | None = None
) -> tuple[R2Storage, MagicMock]:
    """Build ``R2Storage`` with ``_client()`` replaced by a fake yielding a recordable AsyncMock.

    ``enable_checksum_binding=None`` exercises the constructor default;
    ``True``/``False`` set the kwarg explicitly.
    """
    overrides = (
        {}
        if enable_checksum_binding is None
        else {"enable_checksum_binding": enable_checksum_binding}
    )
    storage = R2Storage(**_kwargs(**overrides))
    fake_client = MagicMock()
    fake_client.generate_presigned_url = AsyncMock(return_value=_SIGNED_URL)
    storage._client = lambda: _FakeAsyncClientCM(fake_client)
    return storage, fake_client


class TestGeneratePresignedPutChecksumBinding:
    """Issue #575: assert ``ChecksumSHA256`` flow through ``generate_presigned_put``.

    Pins the Python-side wiring of ``Params["ChecksumSHA256"]`` for the
    flag-on path and its absence for the flag-off path. Complements the
    R2-credentials-gated tests in ``test_r2_live.py`` by exercising the
    conditional in ``r2.py`` without leaving CI.
    """

    @pytest.mark.asyncio
    async def test_flag_on_includes_checksum_in_params(self) -> None:
        storage, fake_client = _make_storage_with_fake_client(enable_checksum_binding=True)

        url = await storage.generate_presigned_put(**_PUT_KWARGS)
        assert url == _SIGNED_URL

        fake_client.generate_presigned_url.assert_awaited_once()
        call_kwargs = fake_client.generate_presigned_url.call_args.kwargs
        assert call_kwargs["ClientMethod"] == "put_object"
        assert call_kwargs["HttpMethod"] == "PUT"
        assert call_kwargs["ExpiresIn"] == _PUT_KWARGS["ttl_seconds"]

        params = call_kwargs["Params"]
        assert params["ChecksumSHA256"] == _SHA256_B64_EMPTY
        assert params["Bucket"] == "kagura-files-test"
        assert params["Key"] == _PUT_KWARGS["key"]
        assert params["ContentType"] == _PUT_KWARGS["content_type"]
        assert params["ContentLength"] == _PUT_KWARGS["size_bytes"]

    @pytest.mark.asyncio
    async def test_flag_off_excludes_checksum_from_params(self) -> None:
        storage, fake_client = _make_storage_with_fake_client(enable_checksum_binding=False)

        await storage.generate_presigned_put(**_PUT_KWARGS)

        params = fake_client.generate_presigned_url.call_args.kwargs["Params"]
        assert "ChecksumSHA256" not in params
        assert params["Bucket"] == "kagura-files-test"
        assert params["Key"] == _PUT_KWARGS["key"]
        assert params["ContentType"] == _PUT_KWARGS["content_type"]
        assert params["ContentLength"] == _PUT_KWARGS["size_bytes"]

    @pytest.mark.asyncio
    async def test_default_flag_matches_explicit_off(self) -> None:
        """Constructor default for ``enable_checksum_binding`` must behave like explicit ``False``.

        Guards against a future settings migration silently flipping the
        default to True — the explicit-False test would still pass in that
        case, so we need this separate exercise of the default branch.
        """
        storage, fake_client = _make_storage_with_fake_client()

        await storage.generate_presigned_put(**_PUT_KWARGS)

        params = fake_client.generate_presigned_url.call_args.kwargs["Params"]
        assert "ChecksumSHA256" not in params
