"""#2980: a context's hybrid fusion setting on ``context_search_configs``.

Adds ``fusion`` (VARCHAR(20) NOT NULL DEFAULT 'auto') and the CHECK
``fusion_check`` (``'auto'``, ``'rrf'``, ``'weighted'``), added ``NOT VALID``
and validated in a transaction of its own.

No backfill UPDATE: every existing row reads ``auto`` through the constant
default (metadata-only on PostgreSQL). With ``HYBRID_FUSION=weighted`` (the
default) ``auto`` fuses exactly as before, so recall results do not change.

Locks: ``ADD COLUMN`` and ``ADD CONSTRAINT ... NOT VALID`` take a brief
``ACCESS EXCLUSIVE`` lock on ``context_search_configs`` only; the VALIDATE
takes ``SHARE UPDATE EXCLUSIVE`` on it (reads and writes go on). No other
table is locked (the foreign key to ``contexts`` is not touched).

The previous release never names the column: its INSERTs get ``auto`` from
the server default, which the CHECK accepts, and its SELECTs list their
columns. Code and migration may therefore be deployed in either order.
Re-runnable (``IF NOT EXISTS`` / ``DROP CONSTRAINT IF EXISTS``).

Downgrade drops the CHECK and the column; every context then fuses by the
deployment's ``HYBRID_FUSION`` again.

Revision ID: f59_2980_context_fusion
Revises: f58_2941_domain_tls_reports
"""

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import validate_constraint

revision: str = "f59_2980_context_fusion"
down_revision: str | None = "f58_2941_domain_tls_reports"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "context_search_configs"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("fusion", sa.String(20), nullable=False, server_default="auto"),
        if_not_exists=True,
    )
    op.execute("ALTER TABLE context_search_configs DROP CONSTRAINT IF EXISTS fusion_check")
    op.execute(
        "ALTER TABLE context_search_configs ADD CONSTRAINT fusion_check "
        "CHECK (fusion IN ('auto', 'rrf', 'weighted')) NOT VALID"
    )
    validate_constraint(_TABLE, "fusion_check")


def downgrade() -> None:
    op.execute("ALTER TABLE context_search_configs DROP CONSTRAINT IF EXISTS fusion_check")
    op.drop_column(_TABLE, "fusion", if_exists=True)
