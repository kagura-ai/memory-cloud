"""Backward-compatible alias for the storage backend (Issue #994).

``R2Storage`` was renamed to :class:`~storage.s3_compatible.S3CompatibleStorage`
when the backend was generalized to any S3-compatible endpoint (R2 / MinIO /
AWS S3). The implementation never branched on R2 — only the endpoint URL
differs — so the rename is behavior-preserving.

This module re-exports the new class under the old name so existing imports
(``from storage.r2 import R2Storage``) and any external references keep working.
Prefer importing :class:`S3CompatibleStorage` from ``storage.s3_compatible`` in
new code.
"""

from __future__ import annotations

from storage.s3_compatible import S3CompatibleStorage

# Backward-compatible alias — same class object, so ``isinstance(x, R2Storage)``
# and ``isinstance(x, S3CompatibleStorage)`` are equivalent.
R2Storage = S3CompatibleStorage

__all__ = ["R2Storage", "S3CompatibleStorage"]
