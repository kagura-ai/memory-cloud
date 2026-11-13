"""SQLAlchemy models for platform-managed file storage (Issue #485).

Two tables:

- ``file_objects`` — authoritative blob registry. One row per uploaded
  file. The ``status`` column carries the upload state machine
  (``reserved → uploaded`` on success, ``→ failed`` on orphan sweep).
- ``workspace_storage_usage`` — per-workspace denormalized counter.
  Updated atomically with ``file_objects`` insert/soft-delete so quota
  checks read a single hot row instead of online ``SUM()`` (same lesson
  as #50 / #136 / #198 around effective quota centralization).

Phase 1 + 1.5 ship R2-only. Phase 2 BYO bucket support
(``byo_s3`` / ``byo_gcs``) does NOT add rows here — those references
live directly in ``Memory.details.external_blob`` with
``backend='byo_*'`` and ``ref=<bucket URI>``.
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
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

#: Malware scan states (Issue #565). ``unscanned``: confirmed while scanning
#: was off (or before it existed); ``pending``: waiting for a verdict;
#: ``clean``; ``infected``: a finding (quarantined); ``error``: the scanner
#: could not check this file (blocked, never deleted automatically).
SCAN_STATUSES = ("unscanned", "pending", "clean", "infected", "error")
#: Why a file is ``error`` (or why the backfill gave up on an ``unscanned`` one).
SCAN_ERRORS = ("object_missing", "digest_mismatch", "size_limit", "unscannable", "scan_failed")
#: ``scan_error`` of a ``pending`` row that was ``unscanned`` (legacy) before a
#: context restore sent it back to the scan: its stored bytes were never checked
#: against its sha256, so a digest mismatch does not block it. Cleared with the
#: verdict.
SCAN_LEGACY_REQUEUED = "legacy_requeued"


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

    The malware scan (Issue #565) keeps its own state in ``scan_status`` and
    the ``scan_*`` columns; ``status`` (and so quota, purge, restore, GC and
    the dedup index) is unchanged by it. The download URL is refused while a
    file is not downloadable by ``services.file_scan_service.ensure_downloadable``.
    """

    __tablename__ = "file_objects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Owning context for intra-workspace access control (Issue #1136). When set,
    # file access (read/write/delete/list) is routed through the context's ACL
    # (private/shared, allowed_context_ids, per-context role) — the same model
    # used by memories — instead of the flat workspace role. NULL = workspace-
    # scoped (legacy behaviour: viewer+ read, member+ write); existing rows are
    # left NULL by the migration (no backfill). Quota + sha256 dedup stay
    # workspace-level (R5); only *access* is context-scoped. ON DELETE SET NULL
    # is a last-resort fallback, NOT an access rule: a NULL'd binding widens the
    # file to workspace scope, so every context hard-delete path must soft-delete
    # bound files first (admin user-erasure does, via
    # FileStorageService.purge_files_for_contexts). Contexts otherwise
    # soft-delete (#84); the workspace hard-delete cascade removes the file row
    # itself anyway.
    context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="SET NULL"),
        nullable=True,
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
        comment="'r2' (only backend in this column's enum CHECK)",
    )
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

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

    # --- Malware scan (Issue #565) ---
    scan_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="unscanned",
        comment="'unscanned' | 'pending' | 'clean' | 'infected' | 'error'",
    )
    #: Claims made (a claim whose scan never reached clamd is given back).
    scan_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: Earliest next claim; a claim moves it to the lease end.
    scan_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: End of the current claim's lease (part of the result fence).
    scan_lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: clamd's finding name (sanitized); never sent to clients.
    scan_signature: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: ETag / LastModified of the object version that was scanned, from the
    #: same GetObject that fed the scan (download-time compare, binding off).
    scan_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scan_last_modified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scan_error: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "storage_backend IN ('r2')",
            name="valid_file_storage_backend",
        ),
        CheckConstraint(
            "status IN ('reserved', 'uploaded', 'failed')",
            name="valid_file_status",
        ),
        # Phase-coherent shape: r2 rows must have a key; reserved rows are
        # exempt because the client may not have completed the PUT yet
        # (storage_key NULL is normal in flight).
        CheckConstraint(
            "(status = 'reserved') OR (storage_backend = 'r2' AND storage_key IS NOT NULL)",
            name="valid_file_storage_shape",
        ),
        CheckConstraint(
            "scan_status IN ('unscanned', 'pending', 'clean', 'infected', 'error')",
            name="valid_file_scan_status",
        ),
        CheckConstraint(
            "scan_error IS NULL OR scan_error IN "
            "('object_missing', 'digest_mismatch', 'size_limit', 'unscannable', 'scan_failed', "
            "'legacy_requeued')",
            name="valid_file_scan_error",
        ),
        Index("ix_file_objects_storage_key", "storage_key"),
        # Active dedup: a workspace can hold one row per sha256 at a time;
        # soft-deleted and failed rows are excluded so a redo upload
        # of a previously-deleted file is allowed.
        # Case-insensitive lower(sha256) mirrors alembic e07_556_sha256_lowercase_index (#556).
        Index(
            "uq_file_objects_workspace_sha256_active",
            "workspace_id",
            text("lower(sha256)"),
            unique=True,
            postgresql_where=(
                "deleted_at IS NULL AND status <> 'failed' AND created_by NOT LIKE 'migration:attachment:%'"
            ),
        ),
        Index(
            "ix_file_objects_legacy_creator",
            "created_by",
            postgresql_where="created_by LIKE 'migration:attachment:%'",
        ),
        # Orphan sweep helper: only matters for in-flight reservations.
        Index(
            "idx_file_objects_reserved_expires",
            "expires_at",
            postgresql_where="status = 'reserved'",
        ),
        # Soft-delete GC helper (Issue #552, broadened in #962): nightly
        # sweep of ``status IN ('uploaded', 'failed') AND deleted_at IS NOT
        # NULL`` rows past the 7-day retention window. #962 added ``failed``:
        # the orphan sweeper now soft-deletes lingering ``failed`` rows
        # (which hold no quota and are UI-hidden but otherwise had no GC
        # path) so this nightly sweep reaps them too. Partial index keeps
        # storage proportional to actually-deleted-pending-GC rows.
        Index(
            "idx_file_objects_soft_deleted_gc",
            "deleted_at",
            postgresql_where=("status IN ('uploaded', 'failed') AND deleted_at IS NOT NULL"),
        ),
        # #962: locator for the orphan sweeper's failed-row soft-delete stamp
        # (and the one-time backfill). Keeps that bulk ``UPDATE … WHERE
        # status='failed' AND deleted_at IS NULL`` an index scan over the
        # normally near-empty failed-pending set — the stamp drains the set
        # each 15-min tick — instead of a seq-scan of the whole table.
        Index(
            "idx_file_objects_failed_pending_softdelete",
            "id",
            postgresql_where="status = 'failed' AND deleted_at IS NULL",
        ),
        # #565 scan queue: due pending scans (a claim moves scan_next_attempt_at
        # to its lease end, so in-flight rows drop out without a lease predicate).
        Index(
            "idx_file_objects_scan_queue",
            "scan_next_attempt_at",
            postgresql_where=(
                "scan_status = 'pending' AND status = 'uploaded' AND deleted_at IS NULL"
            ),
        ),
        # #565 backfill of files uploaded before scanning was on.
        Index(
            "idx_file_objects_scan_backfill",
            "id",
            postgresql_where=(
                "scan_status = 'unscanned' AND status = 'uploaded' AND deleted_at IS NULL"
            ),
        ),
        # #565 quarantine expiry and the blocked-file counts.
        Index(
            "idx_file_objects_quarantine",
            "scanned_at",
            postgresql_where="scan_status IN ('infected', 'error') AND deleted_at IS NULL",
        ),
    )


class WorkspaceStorageUsage(Base):
    """Denormalized per-workspace storage counter (Issue #485).

    Maintained atomically with ``file_objects`` inserts/soft-deletes so
    the quota path reads one row instead of an online ``SUM`` over
    millions of rows. Identical pattern to ``addon_*_bonus`` columns on
    ``Workspace`` — a fast approximation of ``services/storage_counts.py``'s
    reducible figure (#1961, #1966: committed files in live contexts or
    workspace-scoped). It is delta-maintained and floored at 0, never
    reseeded from the rows, so it can drift; the capacity lock and the
    downgrade-eligibility read query the statement directly, and the Redis
    upload counter reseeds from it (``storage_quota_service``). Files left
    in contexts deleted before #1960 are the known drift source, cleared by
    ``cli/backfill_deleted_context_files``.
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


class StorageObjectDeletion(Base):
    """Retry marker surviving file/workspace cascades; no account identifiers."""

    __tablename__ = "storage_object_deletions"

    storage_key: Mapped[str] = mapped_column(String(1024), primary_key=True)
    available_after: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
