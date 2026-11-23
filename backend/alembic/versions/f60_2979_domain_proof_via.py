"""#2979: which record proved control of a custom domain, on ``public_mcp_domains``.

Adds ``last_proof_via`` (VARCHAR(8), nullable) and the CHECK
``ck_public_mcp_domains_last_proof_via`` (``'txt'``, ``'cname'``, ``'both'``),
added ``NOT VALID`` and validated in a transaction of its own. The last check
writes it; it stays NULL while ``MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE`` is
empty.

No backfill: every existing row reads NULL (metadata-only on PostgreSQL).

Locks: ``ADD COLUMN`` and ``ADD CONSTRAINT ... NOT VALID`` take a brief
``ACCESS EXCLUSIVE`` lock on ``public_mcp_domains`` only; the VALIDATE takes
``SHARE UPDATE EXCLUSIVE`` on it. No other table is locked (the table has no
foreign key).

Order: run this migration BEFORE starting the new code. The new code reads
the column wherever it reads domain rows, whatever the switches say. The
previous release never names the column (its INSERTs leave it NULL and its
SELECTs list their columns), so it keeps working against the migrated
schema. Re-runnable (``IF NOT EXISTS`` /
``DROP CONSTRAINT IF EXISTS``).

Downgrade drops the CHECK and the column; the rows and their states stay.

Revision ID: f60_2979_domain_proof_via
Revises: f59_2980_context_fusion
"""

import sqlalchemy as sa

from alembic import op
from db.migration_helpers import validate_constraint

revision: str = "f60_2979_domain_proof_via"
down_revision: str | None = "f59_2980_context_fusion"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "public_mcp_domains"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("last_proof_via", sa.String(8), nullable=True),
        if_not_exists=True,
    )
    op.execute(
        "ALTER TABLE public_mcp_domains DROP CONSTRAINT IF EXISTS "
        "ck_public_mcp_domains_last_proof_via"
    )
    op.execute(
        "ALTER TABLE public_mcp_domains ADD CONSTRAINT ck_public_mcp_domains_last_proof_via "
        "CHECK (last_proof_via IN ('txt', 'cname', 'both')) NOT VALID"
    )
    validate_constraint(_TABLE, "ck_public_mcp_domains_last_proof_via")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public_mcp_domains DROP CONSTRAINT IF EXISTS "
        "ck_public_mcp_domains_last_proof_via"
    )
    op.drop_column(_TABLE, "last_proof_via", if_exists=True)
