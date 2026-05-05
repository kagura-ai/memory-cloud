"""Factory + lifecycle for the in-process ``BlobStorageProtocol`` instance.

Phase 1 dispatches on whether R2 settings are configured:

- All five R2 settings populated → ``R2Storage`` instance.
- Anything missing → raise on first ``get_blob_storage()`` call so dev
  environments without R2 fail fast with a clear error instead of
  reaching production with a half-wired upload path.

Phase 2 BYO will dispatch on a new ``settings.storage_backend_type``
discriminator. Adding it then will be a one-place change here.
"""

from __future__ import annotations

import structlog

from config.settings import get_settings
from storage.protocol import BlobStorageProtocol

logger = structlog.get_logger(__name__)


_storage: BlobStorageProtocol | None = None


def get_blob_storage() -> BlobStorageProtocol:
    """Return the process-wide ``BlobStorageProtocol`` instance.

    Lazily constructed on first call. Raises ``ExternalServiceError``
    (HTTP 502 via the global handler) if R2 is missing OR partially
    configured — both unconfigured (``r2_endpoint_url`` empty) and
    half-configured (some fields set, others empty) surface the same
    "storage unavailable" shape so REST and MCP file handlers can
    return a usable error message instead of an opaque 500.

    Pre-fix: ``RuntimeError`` (unconfigured) and ``ValueError`` (partial)
    leaked unchanged through the file handlers — Copilot finding on
    PR #551 loop 2 (commit 53e8213d → loop 3 fix).
    """
    from utils.exceptions import ExternalServiceError

    global _storage
    if _storage is None:
        settings = get_settings()
        if not settings.r2_endpoint_url:
            raise ExternalServiceError(
                "R2",
                "storage is not configured (R2_ENDPOINT_URL empty). "
                "Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, "
                "R2_BUCKET, and R2_ENDPOINT_URL — see .env.example.",
            )
        from storage.r2 import R2Storage

        try:
            _storage = R2Storage(
                account_id=settings.r2_account_id,
                access_key_id=settings.r2_access_key_id,
                secret_access_key=settings.r2_secret_access_key,
                bucket=settings.r2_bucket,
                endpoint_url=settings.r2_endpoint_url,
            )
        except ValueError as exc:
            raise ExternalServiceError(
                "R2",
                f"storage construction failed (partial config?): {exc}",
            ) from exc
        logger.info("blob_storage_initialized", backend="r2", bucket=settings.r2_bucket)
    return _storage


async def close_blob_storage() -> None:
    """Release the process-wide blob storage instance, if any.

    Safe to call when ``get_blob_storage`` was never invoked.
    """
    global _storage
    if _storage is not None:
        close = getattr(_storage, "close", None)
        if close is not None:
            await close()
        _storage = None
        logger.info("blob_storage_closed")


def _reset_for_tests() -> None:
    """Drop the cached instance — test helpers ONLY.

    Tests that swap ``Settings`` between cases need a way to force a
    re-construction. Production code should never call this.
    """
    global _storage
    _storage = None
