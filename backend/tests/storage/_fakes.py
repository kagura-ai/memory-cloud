"""Shared in-memory ``BlobStorageProtocol`` fake for service-layer tests.

Note: ``tests/storage/test_protocol.py`` keeps its own ``_InMemoryStorage``
on purpose — that file's job is to pin the Protocol contract via
``isinstance(_, BlobStorageProtocol)``, and routing it through an import
would dilute the pin (any contract drift would point at this module
instead of the test file demonstrating the contract).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from storage.protocol import ObjectMetadata


@dataclass(frozen=True)
class FakeOpenedObject:
    """``OpenedObject`` over bytes held in memory (#565)."""

    data: bytes
    etag: str
    size_bytes: int
    last_modified: datetime | None

    async def iter_chunks(self, chunk_size: int) -> AsyncIterator[bytes]:
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start : start + chunk_size]


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
        #: #565: per-key LastModified reported by head/open (naive UTC).
        self.last_modified: dict[str, datetime] = {}

    @asynccontextmanager
    async def open_object(self, key: str) -> AsyncIterator[FakeOpenedObject | None]:
        """#565 reader surface: one consistent snapshot of the object.

        The ETag is the one ``head_object`` reports (the ``sha256`` given to
        ``write_object``), so a test can replace the bytes under the same ETag.
        """
        if key not in self.objects:
            yield None
            return
        data, _, etag = self.objects[key]
        yield FakeOpenedObject(
            data=data,
            etag=etag,
            size_bytes=len(data),
            last_modified=self.last_modified.get(key),
        )

    async def write_object(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        self.objects[key] = (data, content_type, sha256)

    async def head_object(self, key: str) -> ObjectMetadata | None:
        if key not in self.objects:
            return None
        size = self.head_size_override
        if size is None:
            size = len(self.objects[key][0])
        meta: ObjectMetadata = {"size_bytes": size, "etag": self.objects[key][2]}
        if key in self.last_modified:
            meta["last_modified"] = self.last_modified[key]
        return meta

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
