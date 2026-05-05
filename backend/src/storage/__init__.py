"""Object storage abstraction layer (Issue #485).

Phase 1 ships a single concrete backend (Cloudflare R2 via aioboto3),
but the public surface is the ``BlobStorageProtocol`` contract so Phase
2 BYO bucket support (`backend/byo_s3`, `backend/byo_gcs`) plugs in by
adding new implementations without touching ``FileStorageService`` or
the API/MCP route layer.
"""

from storage.factory import close_blob_storage, get_blob_storage
from storage.protocol import BlobStorageProtocol, ObjectMetadata

__all__ = [
    "BlobStorageProtocol",
    "ObjectMetadata",
    "close_blob_storage",
    "get_blob_storage",
]
