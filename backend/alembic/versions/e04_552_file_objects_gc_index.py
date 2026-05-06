"""Add partial index supporting nightly soft-delete GC sweep on file_objects (#552).

Issue #552 introduces a nightly job that scans for ``status='uploaded' AND
deleted_at IS NOT NULL AND deleted_at < now() - 7 days`` rows, deletes the
matching R2 binary, then hard-deletes the row. Without an index the sweep
runs a full table scan every night.

The new partial index narrows storage to rows that are actually candidates
for GC (the dominant ``deleted_at IS NULL AND status='uploaded'`` rows are
excluded from the index entirely).

Revision ID: e04_552_gc_index
Revises: e03_485_file_objects
"""

import sqlalchemy as sa

from alembic import op

revision = "e04_552_gc_index"
down_revision = "e03_485_file_objects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_file_objects_soft_deleted_gc",
        "file_objects",
        ["deleted_at"],
        postgresql_where=sa.text("status = 'uploaded' AND deleted_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_file_objects_soft_deleted_gc", table_name="file_objects")
