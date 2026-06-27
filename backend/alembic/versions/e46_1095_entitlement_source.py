"""Add workspaces.entitlement_source (#1095).

Issue #1095: a periodic reconciliation against the external billing source must
not revert legitimate admin/comp grants. ``entitlement_source`` records WHO last
set the entitlement so the external reconciler (kagura-billing#5) reverts only
what it owns:

- ``external_billing`` — billing-owned (set via the internal endpoint); reconcilable.
- ``admin_grant`` — locally-owned (admin/comp grant or owner self-service); the
  reconciler MUST NOT revert it.

NOT NULL with an ``admin_grant`` server_default so every existing row backfills
**protectively** (no data migration, no comp grant ever reverted on the first
reconcile). An external-billing workspace self-heals to ``external_billing`` on
its next push from the billing service.

Revision ID: e46_1095_entitlement_src
Revises: e45_1094_ownership_epoch

Note: the revision id is kept <=32 chars for the ``alembic_version.version_num``
VARCHAR(32) column — same convention as the sibling capped migrations.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e46_1095_entitlement_src"
down_revision = "e45_1094_ownership_epoch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "entitlement_source",
            sa.String(length=32),
            nullable=False,
            server_default="admin_grant",
        ),
    )
    op.create_check_constraint(
        "valid_entitlement_source",
        "workspaces",
        "entitlement_source IN ('external_billing', 'admin_grant')",
    )


def downgrade() -> None:
    op.drop_constraint("valid_entitlement_source", "workspaces", type_="check")
    op.drop_column("workspaces", "entitlement_source")
