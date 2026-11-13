"""Blob-storage Protocol for Issue #485 (#485 R1).

Structural typing — implementations need only define the methods, no
inheritance. Phase 1 ships ``R2Storage``; Phase 2 adds ``S3Storage`` and
``GCSStorage`` (BYO bucket) without touching ``FileStorageService``.

All operations are async because the dominant call sites
(``upload-init``, ``upload-complete``, ``download``, soft-delete) are
inside FastAPI request handlers. ``generate_presigned_*`` is technically
synchronous in boto3 / aioboto3, but is exposed as async so callers
need not branch on backend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import NotRequired, Protocol, TypedDict, runtime_checkable


class ObjectMetadata(TypedDict):
    """Subset of object metadata returned by ``head_object``.

    R2 / S3-compatible backends return many more fields (etag, version
    id, server-side encryption, etc.); only the ones used by the upload
    confirmation flow are typed here. Implementations may include
    additional keys.
    """

    size_bytes: int
    etag: str
    last_modified: NotRequired[datetime]


@runtime_checkable
class BlobStorageProtocol(Protocol):
    """Contract for object-storage backends used by ``FileStorageService``."""

    async def write_object(
        self,
        key: str,
        data: bytes,
        content_type: str,
        sha256: str,
    ) -> None:
        """Upload ``data`` to ``key`` with ``content_type`` metadata.

        Used by the legacy-attachment migration step (Commit 9). The
        normal upload flow uses ``generate_presigned_put`` instead so
        bytes never traverse the platform.
        """
        ...

    async def head_object(self, key: str) -> ObjectMetadata | None:
        """Return ``ObjectMetadata`` if the object exists, ``None`` otherwise.

        Implementations MUST return ``None`` (not raise) for missing
        objects — used by ``upload-complete`` to verify a presigned PUT
        actually landed.
        """
        ...

    async def delete_object(self, key: str) -> None:
        """Delete ``key``. No-op if the object is already missing."""
        ...

    async def generate_presigned_put(
        self,
        key: str,
        content_type: str,
        size_bytes: int,
        ttl_seconds: int,
        sha256: str,
    ) -> str:
        """Return a short-lived presigned PUT URL clients can upload to.

        ``sha256`` is the hex-encoded sha256 of the bytes the client
        intends to upload. Implementations SHOULD sign the URL such that
        the storage backend rejects bytes whose actual digest differs
        from this value (S3 Object Integrity / `x-amz-checksum-sha256`).
        This closes the per-workspace dedup-poisoning gap from #485
        Phase 1 — the platform no longer trusts the caller's
        ``confirm_upload`` sha256 claim, the storage layer enforces it.

        Implementations MAY make this enforcement opt-in via a deploy
        flag (R2 does so via ``r2_checksum_binding_enabled``) so a
        backend can roll forward ahead of the SDK release that sends
        the matching ``x-amz-checksum-sha256`` header.

        ``size_bytes`` is reflected as a `Content-Length` constraint
        when the backend supports it.
        """
        ...

    async def generate_presigned_get(
        self,
        key: str,
        filename: str,
        ttl_seconds: int,
    ) -> str:
        """Return a short-lived presigned GET URL.

        ``filename`` is set as the ``Content-Disposition`` so the
        browser uses the original filename instead of the storage key.
        """
        ...


class StoredObject(TypedDict):
    """Inventory entry. Timestamps are naive UTC, like file registry dates."""

    key: str
    last_modified: datetime


class BlobInventoryProtocol(BlobStorageProtocol, Protocol):
    """Optional inventory surface for the operator orphan sweeper."""

    def iter_objects(self) -> AsyncIterator[StoredObject]: ...


class OpenedObject(Protocol):
    """One GetObject response being read (Issue #565).

    ``etag``, ``size_bytes`` and ``last_modified`` describe the object version
    whose bytes ``iter_chunks`` yields (the same response), so a verdict on
    those bytes can be compared with a later ``head_object``.
    ``last_modified`` is naive UTC, ``None`` when the backend sent none.
    """

    @property
    def etag(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def last_modified(self) -> datetime | None: ...

    def iter_chunks(self, chunk_size: int) -> AsyncIterator[bytes]: ...


@runtime_checkable
class BlobReaderProtocol(BlobStorageProtocol, Protocol):
    """Optional read surface for the upload malware scan (Issue #565).

    The scan is the first server-side reader of uploaded bytes. The context
    manager yields ``None`` for a missing object and raises
    ``ExternalServiceError`` for any other storage failure; the body is
    released when the block exits. Implementations never log the key.
    """

    def open_object(self, key: str) -> AbstractAsyncContextManager[OpenedObject | None]: ...
