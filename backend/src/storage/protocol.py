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

from typing import Protocol, TypedDict, runtime_checkable


class ObjectMetadata(TypedDict):
    """Subset of object metadata returned by ``head_object``.

    R2 / S3-compatible backends return many more fields (etag, version
    id, server-side encryption, etc.); only the ones used by the upload
    confirmation flow are typed here. Implementations may include
    additional keys.
    """

    size_bytes: int
    etag: str


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
    ) -> str:
        """Return a short-lived presigned PUT URL clients can upload to.

        ``size_bytes`` is reflected as a `Content-Length` constraint
        when the backend supports it; R2 currently does not enforce
        this server-side, so the platform must validate via
        ``head_object`` after the upload.
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
