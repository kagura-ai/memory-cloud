"""SQLAlchemy models for platform-managed file storage (Issue #485).

Two tables:

- ``file_objects`` — authoritative blob registry. One row per uploaded
  file. The ``status`` column carries the upload state machine
  (``reserved → uploaded`` on success, ``→ failed`` on orphan sweep).
- ``workspace_storage_usage`` — per-workspace denormalized counter.
  Updated atomically with ``file_objects`` insert/soft-delete so quota
  checks read a single hot row instead of online ``SUM()`` (same lesson
  as #50 / #136 / #198 around effective quota centralization).

Phase 1 only writes ``storage_backend='r2'`` rows. ``inline_bytes`` /
``pg_inline`` is reserved Phase 1.5; the CHECK constraint already
covers both shapes so no ALTER is needed when the inline path lands.

Phase 2 BYO bucket support (``byo_s3`` / ``byo_gcs``) does NOT add rows
here — those references live directly in ``Memory.details.external_blob``
with ``backend='byo_*'`` and ``ref=<bucket URI>``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class FileObject(Base):
    """One row per uploaded file (Issue #485).

    The upload flow is:

    1. ``upload-init`` inserts a row with ``status='reserved'`` and a
       short-lived presigned PUT URL.
    2. The client PUTs bytes directly to R2 using the presigned URL.
    3. ``upload-complete`` verifies the object via ``head_object`` and
       transitions ``reserved → uploaded``.
    4. If the client never confirms, the orphan sweeper transitions
       ``reserved → failed`` after the TTL grace and releases the
       reserved quota bytes.
    """

    __tablename__ = "file_objects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)

    storage_backend: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="r2",
        comment="'r2' (Phase 1), 'pg_inline' (reserved Phase 1.5)",
    )
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    inline_bytes: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="reserved",
        comment="'reserved' | 'uploaded' | 'failed'",
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "storage_backend IN ('r2', 'pg_inline')",
            name="valid_file_storage_backend",
        ),
        CheckConstraint(
            "status IN ('reserved', 'uploaded', 'failed')",
            name="valid_file_status",
        ),
        # Phase-coherent shape: r2 rows must have a key; pg_inline rows must
        # have inline bytes; reserved rows are exempt because the client may
        # not have completed the PUT yet (storage_key NULL is normal in flight).
        CheckConstraint(
            (
                "(status = 'reserved') "
                "OR (storage_backend = 'r2' "
                "    AND storage_key IS NOT NULL "
                "    AND inline_bytes IS NULL) "
                "OR (storage_backend = 'pg_inline' "
                "    AND storage_key IS NULL "
                "    AND inline_bytes IS NOT NULL)"
            ),
            name="valid_file_storage_shape",
        ),
        # Active dedup: a workspace can hold one row per sha256 at a time;
        # soft-deleted and failed rows are excluded so a redo upload
        # of a previously-deleted file is allowed.
        Index(
            "uq_file_objects_workspace_sha256_active",
            "workspace_id",
            "sha256",
            unique=True,
            postgresql_where=("deleted_at IS NULL AND status <> 'failed'"),
        ),
        # Orphan sweep helper: only matters for in-flight reservations.
        Index(
            "idx_file_objects_reserved_expires",
            "expires_at",
            postgresql_where="status = 'reserved'",
        ),
        # Soft-delete GC helper (Issue #552): nightly sweep of
        # ``status='uploaded' AND deleted_at IS NOT NULL`` rows past the
        # 7-day retention window. Partial index keeps storage proportional
        # to actually-deleted-pending-GC rows rather than total uploads.
        Index(
            "idx_file_objects_soft_deleted_gc",
            "deleted_at",
            postgresql_where="status = 'uploaded' AND deleted_at IS NOT NULL",
        ),
    )


class WorkspaceStorageUsage(Base):
    """Denormalized per-workspace storage counter (Issue #485).

    Maintained atomically with ``file_objects`` inserts/soft-deletes so
    the quota path reads one row instead of an online ``SUM`` over
    millions of rows. Identical pattern to ``addon_*_bonus`` columns on
    ``Workspace`` — single source of truth for fast reads.
    """

    __tablename__ = "workspace_storage_usage"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("used_bytes >= 0", name="nonneg_used_bytes"),
        CheckConstraint("file_count >= 0", name="nonneg_file_count"),
    )
