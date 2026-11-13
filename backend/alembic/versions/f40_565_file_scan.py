"""#565: malware scan state on ``file_objects``.

Adds the scan columns (``scan_status`` defaulting to ``'unscanned'``,
``scan_attempts``, ``scan_next_attempt_at``, ``scan_lease_until``,
``scanned_at``, ``scan_signature``, ``scan_etag``, ``scan_last_modified``,
``scan_error``), the two CHECKs (added ``NOT VALID`` and validated in a
transaction of their own) and three partial indexes built ``CONCURRENTLY``
(the scan queue, the backfill of unscanned files, the quarantine).

No backfill UPDATE: every existing row becomes ``unscanned`` through the
constant default (metadata-only on PostgreSQL), which is downloadable as
before. ``status`` and the dedup index are unchanged. Re-runnable end to end
(``IF NOT EXISTS`` everywhere, an index left INVALID by an interrupted build
is dropped and built again).

Downgrade is refused while a row is ``infected`` or ``error``, a soft-deleted
one included (a context restore can bring it back): dropping the columns would
make those blocked files downloadable again. Clear them first: a live file can
be rescanned (the ``file_scan`` CLI) or deleted for good; a soft-deleted one is
skipped by the rescan, so delete it for good or leave it to the soft-deleted
file garbage collection after its retention window. Downgrade then.

Revision ID: f40_565_file_scan
Revises: f39_2768_operations_status
"""

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import index_is_invalid, validate_constraint

revision: str = "f40_565_file_scan"
down_revision: str | None = "f39_2768_operations_status"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "file_objects"

_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "idx_file_objects_scan_queue",
        "scan_next_attempt_at",
        "scan_status = 'pending' AND status = 'uploaded' AND deleted_at IS NULL",
    ),
    (
        "idx_file_objects_scan_backfill",
        "id",
        "scan_status = 'unscanned' AND status = 'uploaded' AND deleted_at IS NULL",
    ),
    (
        "idx_file_objects_quarantine",
        "scanned_at",
        "scan_status IN ('infected', 'error') AND deleted_at IS NULL",
    ),
)


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "scan_status",
            sa.String(20),
            nullable=False,
            server_default="unscanned",
            comment="'unscanned' | 'pending' | 'clean' | 'infected' | 'error'",
        ),
        if_not_exists=True,
    )
    op.add_column(
        _TABLE,
        sa.Column("scan_attempts", sa.Integer(), nullable=False, server_default="0"),
        if_not_exists=True,
    )
    for name in ("scan_next_attempt_at", "scan_lease_until", "scanned_at", "scan_last_modified"):
        op.add_column(_TABLE, sa.Column(name, sa.DateTime(), nullable=True), if_not_exists=True)
    for name in ("scan_signature", "scan_etag"):
        op.add_column(_TABLE, sa.Column(name, sa.String(255), nullable=True), if_not_exists=True)
    op.add_column(_TABLE, sa.Column("scan_error", sa.String(32), nullable=True), if_not_exists=True)

    op.execute("ALTER TABLE file_objects DROP CONSTRAINT IF EXISTS valid_file_scan_status")
    op.execute(
        "ALTER TABLE file_objects ADD CONSTRAINT valid_file_scan_status "
        "CHECK (scan_status IN ('unscanned', 'pending', 'clean', 'infected', 'error')) NOT VALID"
    )
    op.execute("ALTER TABLE file_objects DROP CONSTRAINT IF EXISTS valid_file_scan_error")
    op.execute(
        "ALTER TABLE file_objects ADD CONSTRAINT valid_file_scan_error "
        "CHECK (scan_error IS NULL OR scan_error IN "
        "('object_missing', 'digest_mismatch', 'size_limit', 'unscannable', 'scan_failed', "
        "'legacy_requeued')) "
        "NOT VALID"
    )
    validate_constraint(_TABLE, "valid_file_scan_status")
    validate_constraint(_TABLE, "valid_file_scan_error")

    for name, column, where in _INDEXES:
        invalid = index_is_invalid(name)
        with op.get_context().autocommit_block():
            if invalid:
                op.drop_index(name, table_name=_TABLE, postgresql_concurrently=True)
            op.create_index(
                name,
                _TABLE,
                [column],
                postgresql_where=sa.text(where),
                postgresql_concurrently=True,
                if_not_exists=True,
            )


def downgrade() -> None:
    """Refused while ``infected`` / ``error`` rows exist, soft-deleted ones
    included (they would become downloadable, after a context restore too).
    The check runs under ``ACCESS EXCLUSIVE`` on ``file_objects`` before
    anything is dropped, in the transaction that drops the CHECKs and
    the columns: no blocked row can arrive between the check and the drop, and
    a refusal rolls it all back. The indexes go first, only after the check
    passed, in that same locked transaction (a plain ``DROP INDEX``, not
    ``CONCURRENTLY``: the columns' drop would remove them anyway).
    """
    bind = op.get_bind()
    op.execute("LOCK TABLE file_objects IN ACCESS EXCLUSIVE MODE")
    has_column = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'file_objects' AND column_name = 'scan_status'"
        )
    ).first()
    if has_column is not None:
        blocked = bind.execute(
            sa.text("SELECT 1 FROM file_objects WHERE scan_status IN ('infected', 'error') LIMIT 1")
        ).first()
        if blocked is not None:
            raise RuntimeError(
                "f40_565_file_scan downgrade refused: file_objects holds rows the "
                "malware scan blocked (scan_status 'infected' or 'error', soft-deleted "
                "ones included: a context restore can bring them back); dropping the "
                "scan columns would make them downloadable again. Rescan the live "
                "ones (the rescan needs FILE_SCAN_ENABLED=true) or delete them for "
                "good; the rescan skips soft-deleted ones, "
                "so delete those for good or let the soft-deleted file garbage "
                "collection remove them after their retention window (#565)"
            )
    op.execute("ALTER TABLE file_objects DROP CONSTRAINT IF EXISTS valid_file_scan_error")
    op.execute("ALTER TABLE file_objects DROP CONSTRAINT IF EXISTS valid_file_scan_status")
    for name, _, _ in _INDEXES:
        op.drop_index(name, table_name=_TABLE, if_exists=True)
    for column in (
        "scan_error",
        "scan_last_modified",
        "scan_etag",
        "scan_signature",
        "scanned_at",
        "scan_lease_until",
        "scan_next_attempt_at",
        "scan_attempts",
        "scan_status",
    ):
        op.drop_column(_TABLE, column, if_exists=True)
