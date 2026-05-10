"""Shared in-memory ``BlobStorageProtocol`` fake for service-layer tests.

Note: ``tests/storage/test_protocol.py`` keeps its own ``_InMemoryStorage``
on purpose — that file's job is to pin the Protocol contract via
``isinstance(_, BlobStorageProtocol)``, and routing it through an import
would dilute the pin (any contract drift would point at this module
instead of the test file demonstrating the contract).
"""

from __future__ import annotations

from storage.protocol import ObjectMetadata


class FakeBlobStorage:
    """In-memory fake conforming to ``BlobStorageProtocol``.

    ``head_size_override`` is an opt-in test seam: when set to an int,
    ``head_object`` reports that size instead of the actual byte length.
    Used by truncation-refund tests to simulate a backend that reports a
    different size than the bytes the client claims to have uploaded.
    Default ``None`` means ``head_object`` returns the true size — i.e.
    setting this attribute is the only way to deviate from truthful
    behaviour, and tests that do not touch it cannot be silently affected.
    """

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.head_size_override: int | None = None

    async def write_object(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        self.objects[key] = (data, content_type, sha256)

    async def head_object(self, key: str) -> ObjectMetadata | None:
        if key not in self.objects:
            return None
        size = self.head_size_override
        if size is None:
            size = len(self.objects[key][0])
        return {"size_bytes": size, "etag": self.objects[key][2]}

    async def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    async def generate_presigned_put(
        self, key: str, content_type: str, size_bytes: int, ttl_seconds: int, sha256: str
    ) -> str:
        return (
            f"https://test.local/put/{key}?size={size_bytes}&ttl={ttl_seconds}&sha256={sha256[:8]}"
        )

    async def generate_presigned_get(self, key: str, filename: str, ttl_seconds: int) -> str:
        return f"https://test.local/get/{key}?file={filename}&ttl={ttl_seconds}"
