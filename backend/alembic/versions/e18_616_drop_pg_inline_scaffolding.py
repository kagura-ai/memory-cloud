"""Drop dead pg_inline scaffolding from file_objects (#616).

#485 R1 decided R2 over PG bytea for blob storage. Phase 1 shipped
R2-only (PR #551, v0.15.0); Phase 1.5 was hardening (no inline path);
Phase 2 plan is BYO bucket (``byo_s3`` / ``byo_gcs``) via
``Memory.details.external_blob``, not ``pg_inline``. The ``inline_bytes``
column + ``'pg_inline'`` enum value + matching CHECK clauses are residual
scaffolding from before R1 was finalized. This migration removes them so
the schema reflects the actual planned architecture.

No production row has ever had ``storage_backend = 'pg_inline'`` or a
non-NULL ``inline_bytes`` — the service-layer always passes
``storage_backend='r2'`` and ``inline_bytes=None`` (verified via
``backend/src/services/file_storage_service.py:265-267,550-552``). The
drop is therefore data-safe.

Upgrade:
1. Drop the ``valid_file_storage_shape`` CHECK (it references both
   ``storage_backend`` and ``inline_bytes`` — must drop before the column).
2. Drop the ``valid_file_storage_backend`` CHECK (depends on the
   ``storage_backend`` value-set being widened).
3. Drop the ``inline_bytes`` column.
4. Re-add ``valid_file_storage_backend`` with the R2-only enum.
5. Re-add ``valid_file_storage_shape`` with the R2-only shape
   (``reserved`` OR ``r2 + storage_key IS NOT NULL``).

Downgrade is reversible: re-adds the column as NULL-able BYTEA and
restores both CHECKs to the e03_485 shape. Data cannot be restored
(none ever existed), so this is a pure DDL reversal.

Revision ID: e18_616_drop_pg_inline  (22 chars — within VARCHAR(32))
Revises: e17_722_neural_edge_origin
"""

import sqlalchemy as sa

from alembic import op

revision = "e18_616_drop_pg_inline"
down_revision = "e17_722_neural_edge_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop pg_inline scaffolding; tighten CHECKs to R2-only."""
    op.drop_constraint(
        "valid_file_storage_shape",
        "file_objects",
        type_="check",
    )
    op.drop_constraint(
        "valid_file_storage_backend",
        "file_objects",
        type_="check",
    )
    op.drop_column("file_objects", "inline_bytes")
    op.create_check_constraint(
        "valid_file_storage_backend",
        "file_objects",
        "storage_backend IN ('r2')",
    )
    op.create_check_constraint(
        "valid_file_storage_shape",
        "file_objects",
        "(status = 'reserved') OR (storage_backend = 'r2' AND storage_key IS NOT NULL)",
    )


def downgrade() -> None:
    """Restore inline_bytes column and the original CHECK clauses.

    The data that ever existed in inline_bytes was none; this is pure
    DDL reversal.
    """
    op.drop_constraint(
        "valid_file_storage_shape",
        "file_objects",
        type_="check",
    )
    op.drop_constraint(
        "valid_file_storage_backend",
        "file_objects",
        type_="check",
    )
    op.add_column(
        "file_objects",
        sa.Column(
            "inline_bytes",
            sa.dialects.postgresql.BYTEA(),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "valid_file_storage_backend",
        "file_objects",
        "storage_backend IN ('r2', 'pg_inline')",
    )
    op.create_check_constraint(
        "valid_file_storage_shape",
        "file_objects",
        "(status = 'reserved') "
        "OR (storage_backend = 'r2' "
        "    AND storage_key IS NOT NULL "
        "    AND inline_bytes IS NULL) "
        "OR (storage_backend = 'pg_inline' "
        "    AND storage_key IS NULL "
        "    AND inline_bytes IS NOT NULL)",
    )
