"""Add file_objects + workspace_storage_usage + Memory.external_blob computed columns (#485).

Issue #485 Phase 1 schema:

- ``file_objects``: authoritative blob registry with the upload state
  machine (``reserved | uploaded | failed``), partial-unique sha256
  dedup per workspace, sweeper helper index on reserved expiries.
- ``workspace_storage_usage``: denormalized per-workspace counter
  updated atomically with ``file_objects`` insert/soft-delete to keep
  quota reads off online ``SUM`` queries.
- ``memories.external_blob_backend`` and ``memories.external_blob_ref``:
  generated columns extracted from ``details->'external_blob'->>(...)``.
  Mirrors the existing ``resource_id``/``resource_doc_id``/``resource_version``
  Computed pattern. Partial btree on ``external_blob_ref`` (nullable
  rows excluded) keeps the index size proportional to actual blob
  usage rather than total memory rows.

Revision ID: e03_485_file_objects
Revises: e02_485_addon_storage_mb
"""

import sqlalchemy as sa

from alembic import op

revision = "e03_485_file_objects"
down_revision = "e02_485_addon_storage_mb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create file_objects + workspace_storage_usage + Memory generated cols."""

    # ----- file_objects ---------------------------------------------------
    op.create_table(
        "file_objects",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("storage_backend", sa.String(20), nullable=False, server_default="r2"),
        sa.Column("storage_key", sa.String(1024), nullable=True),
        sa.Column("inline_bytes", sa.dialects.postgresql.BYTEA(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="reserved"),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("uploaded_at", sa.DateTime, nullable=True),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.CheckConstraint(
            "storage_backend IN ('r2', 'pg_inline')",
            name="valid_file_storage_backend",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'uploaded', 'failed')",
            name="valid_file_status",
        ),
        sa.CheckConstraint(
            "(status = 'reserved') "
            "OR (storage_backend = 'r2' "
            "    AND storage_key IS NOT NULL "
            "    AND inline_bytes IS NULL) "
            "OR (storage_backend = 'pg_inline' "
            "    AND storage_key IS NULL "
            "    AND inline_bytes IS NOT NULL)",
            name="valid_file_storage_shape",
        ),
    )

    op.create_index(
        "ix_file_objects_workspace_id",
        "file_objects",
        ["workspace_id"],
    )
    op.create_index(
        "uq_file_objects_workspace_sha256_active",
        "file_objects",
        ["workspace_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND status <> 'failed'"),
    )
    op.create_index(
        "idx_file_objects_reserved_expires",
        "file_objects",
        ["expires_at"],
        postgresql_where=sa.text("status = 'reserved'"),
    )

    # ----- workspace_storage_usage ---------------------------------------
    op.create_table(
        "workspace_storage_usage",
        sa.Column(
            "workspace_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("used_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("file_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("used_bytes >= 0", name="nonneg_used_bytes"),
        sa.CheckConstraint("file_count >= 0", name="nonneg_file_count"),
    )

    # ----- Memory.external_blob computed columns -------------------------
    op.execute(
        """
        ALTER TABLE memories
        ADD COLUMN external_blob_backend VARCHAR(50)
        GENERATED ALWAYS AS (details->'external_blob'->>'backend') STORED
        """
    )
    op.execute(
        """
        ALTER TABLE memories
        ADD COLUMN external_blob_ref VARCHAR(2048)
        GENERATED ALWAYS AS (details->'external_blob'->>'ref') STORED
        """
    )

    op.create_index(
        "idx_memories_external_blob_ref",
        "memories",
        ["external_blob_ref"],
        postgresql_where=sa.text("external_blob_ref IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop everything in reverse order."""
    op.drop_index("idx_memories_external_blob_ref", table_name="memories")
    op.drop_column("memories", "external_blob_ref")
    op.drop_column("memories", "external_blob_backend")
    op.drop_table("workspace_storage_usage")
    op.drop_index("idx_file_objects_reserved_expires", table_name="file_objects")
    op.drop_index("uq_file_objects_workspace_sha256_active", table_name="file_objects")
    op.drop_index("ix_file_objects_workspace_id", table_name="file_objects")
    op.drop_table("file_objects")
