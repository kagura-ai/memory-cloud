"""Tests for ``BlobStorageProtocol`` (Issue #485).

Protocol-shape contract: any class that implements the five required
methods passes ``isinstance`` against ``BlobStorageProtocol`` thanks to
``@runtime_checkable``. This is what lets ``FileStorageService`` accept
either the production ``R2Storage`` or a test fake without explicit
inheritance, and what guards us against silently breaking the contract
in Phase 2 when BYO impls land.
"""

from __future__ import annotations

import pytest

from storage.protocol import BlobStorageProtocol, ObjectMetadata


class _InMemoryStorage:
    """Minimal Protocol-conforming impl for tests.

    Stores objects in a dict; presigned URLs return synthetic strings.
    Mirrors the surface ``R2Storage`` exposes so tests at higher layers
    can swap this in without touching aioboto3 / R2 at all.
    """

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}

    async def write_object(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        self.objects[key] = (data, content_type, sha256)

    async def head_object(self, key: str) -> ObjectMetadata | None:
        if key not in self.objects:
            return None
        data, _, sha256 = self.objects[key]
        return ObjectMetadata(size_bytes=len(data), etag=sha256)

    async def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    async def generate_presigned_put(
        self, key: str, content_type: str, size_bytes: int, ttl_seconds: int, sha256: str
    ) -> str:
        return f"https://test.local/put/{key}?ttl={ttl_seconds}&sha256={sha256[:8]}"

    async def generate_presigned_get(self, key: str, filename: str, ttl_seconds: int) -> str:
        return f"https://test.local/get/{key}?file={filename}&ttl={ttl_seconds}"


class TestProtocolContract:
    def test_in_memory_storage_satisfies_protocol(self):
        """Any object exposing the 5 required async methods passes
        isinstance against the runtime-checkable Protocol."""
        storage = _InMemoryStorage()
        assert isinstance(storage, BlobStorageProtocol)

    def test_object_missing_method_does_not_satisfy_protocol(self):
        """An impl missing one of the 5 required methods MUST fail
        isinstance — this is what catches Protocol drift in Phase 2."""

        class IncompleteStorage:
            async def write_object(
                self, key: str, data: bytes, content_type: str, sha256: str
            ) -> None:
                pass

            # missing head_object, delete_object, generate_presigned_*

        assert not isinstance(IncompleteStorage(), BlobStorageProtocol)


class TestInMemoryStorageRoundtrip:
    """Sanity checks on the in-memory fake itself (used as fixture in
    higher-layer tests)."""

    @pytest.mark.asyncio
    async def test_write_then_head_returns_metadata(self):
        storage = _InMemoryStorage()
        await storage.write_object(
            "ws/abc/file.bin", b"hello", "application/octet-stream", "0xdead"
        )
        meta = await storage.head_object("ws/abc/file.bin")
        assert meta is not None
        assert meta["size_bytes"] == 5
        assert meta["etag"] == "0xdead"

    @pytest.mark.asyncio
    async def test_head_missing_returns_none(self):
        storage = _InMemoryStorage()
        assert await storage.head_object("ws/abc/missing") is None

    @pytest.mark.asyncio
    async def test_delete_is_idempotent(self):
        storage = _InMemoryStorage()
        await storage.write_object("ws/abc/x.bin", b"x", "text/plain", "0x1")
        await storage.delete_object("ws/abc/x.bin")
        await storage.delete_object("ws/abc/x.bin")  # second call: must not raise
        assert await storage.head_object("ws/abc/x.bin") is None

    @pytest.mark.asyncio
    async def test_presigned_urls_include_key_and_ttl(self):
        storage = _InMemoryStorage()
        # Use a realistic 64-char hex digest so the fake's signature
        # matches the real on-wire contract — passing "0xdead" here would
        # make this test pass against a fake whose validation has drifted
        # away from the production protocol shape.
        sha256 = "a" * 64
        put_url = await storage.generate_presigned_put(
            "ws/abc/y.pdf", "application/pdf", 1024, 300, sha256
        )
        assert "ws/abc/y.pdf" in put_url
        assert "ttl=300" in put_url

        get_url = await storage.generate_presigned_get("ws/abc/y.pdf", "report.pdf", 60)
        assert "ws/abc/y.pdf" in get_url
        assert "file=report.pdf" in get_url
        assert "ttl=60" in get_url
