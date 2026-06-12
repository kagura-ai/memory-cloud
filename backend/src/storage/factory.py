"""Factory + lifecycle for the in-process ``BlobStorageProtocol`` instance.

Dispatch (Issue #485; generalized in #994):

- ``settings.storage_backend_type`` selects the backend label. Every known
  value (r2 / s3 / minio / s3-compatible / aws) currently constructs the same
  :class:`~storage.s3_compatible.S3CompatibleStorage` — they all speak the S3
  API and differ only by ``STORAGE_ENDPOINT_URL``. The discriminator exists so
  self-hosters can declare their backend explicitly (and so a future per-backend
  impl is a one-place change here).
- An unknown ``storage_backend_type`` or an empty endpoint raises
  ``ExternalServiceError`` (HTTP 502) on the first ``get_blob_storage()`` call,
  so a half-wired upload path fails fast with a clear error, never an opaque 500.

Backward compatibility: prod sets only the legacy ``R2_*`` env vars and no
discriminator. The default ``storage_backend_type="r2"`` plus the ``R2_*``
``AliasChoices`` on the settings fields keep that deploy working unchanged; a
one-time deprecation line is logged when only ``R2_*`` (no ``STORAGE_*``) is set.
"""

from __future__ import annotations

import os

import structlog

from config.settings import get_settings
from storage.protocol import BlobStorageProtocol

logger = structlog.get_logger(__name__)

# Known discriminator values. All map to S3CompatibleStorage today; the set is
# the validation allowlist so a typo (e.g. "mino") fails loudly, not silently.
_KNOWN_BACKENDS = frozenset({"r2", "s3", "minio", "s3-compatible", "aws"})


def _warn_if_legacy_storage_env() -> None:
    """Emit one deprecation line when the deploy uses only legacy ``R2_*`` env.

    Best-effort and side-effect-free beyond logging: reads ``os.environ``
    directly (the resolved Settings value cannot report which alias matched).
    Silent when canonical ``STORAGE_*`` / ``S3_*`` vars are present.
    """
    legacy = any(
        os.getenv(k)
        for k in (
            "R2_ENDPOINT_URL",
            "R2_BUCKET",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_ACCOUNT_ID",
        )
    )
    canonical = any(
        os.getenv(k)
        for k in (
            "STORAGE_ENDPOINT_URL",
            "S3_ENDPOINT_URL",
            "STORAGE_BUCKET",
            "S3_BUCKET",
            "STORAGE_ACCESS_KEY_ID",
            "S3_ACCESS_KEY_ID",
            "STORAGE_SECRET_ACCESS_KEY",
            "S3_SECRET_ACCESS_KEY",
            "STORAGE_ACCOUNT_ID",
            "S3_ACCOUNT_ID",
        )
    )
    if legacy and not canonical:
        logger.warning(
            "storage_env_deprecated",
            detail=(
                "R2_* storage env vars are deprecated; rename to STORAGE_* "
                "(R2_* is still honored). See docs/deployment.md."
            ),
        )


_storage: BlobStorageProtocol | None = None


def get_blob_storage() -> BlobStorageProtocol:
    """Return the process-wide ``BlobStorageProtocol`` instance.

    Lazily constructed on first call. Raises ``ExternalServiceError``
    (HTTP 502 via the global handler) if storage is missing OR partially
    configured — both unconfigured (``storage_endpoint_url`` empty) and
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

        backend_type = (settings.storage_backend_type or "r2").strip().lower()
        if backend_type not in _KNOWN_BACKENDS:
            raise ExternalServiceError(
                "storage",
                f"unknown storage_backend_type '{backend_type}' "
                f"(expected one of: {', '.join(sorted(_KNOWN_BACKENDS))}).",
            )

        if not settings.storage_endpoint_url:
            raise ExternalServiceError(
                "storage",
                "storage is not configured (STORAGE_ENDPOINT_URL empty). "
                "Set STORAGE_ACCESS_KEY_ID, STORAGE_SECRET_ACCESS_KEY, "
                "STORAGE_BUCKET, and STORAGE_ENDPOINT_URL (legacy R2_* names are "
                "still accepted) — see .env.example.",
            )

        _warn_if_legacy_storage_env()

        from storage.s3_compatible import S3CompatibleStorage

        try:
            _storage = S3CompatibleStorage(
                account_id=settings.storage_account_id,
                access_key_id=settings.storage_access_key_id,
                secret_access_key=settings.storage_secret_access_key,
                bucket=settings.storage_bucket,
                endpoint_url=settings.storage_endpoint_url,
                enable_checksum_binding=settings.storage_checksum_binding_enabled,
            )
        except ValueError as exc:
            raise ExternalServiceError(
                "storage",
                f"storage construction failed (partial config?): {exc}",
            ) from exc
        logger.info(
            "blob_storage_initialized",
            backend=backend_type,
            bucket=settings.storage_bucket,
        )
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
