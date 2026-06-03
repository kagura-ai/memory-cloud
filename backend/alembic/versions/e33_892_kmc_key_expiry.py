"""Add kmc_api_key_expires_at to workspace_connectors (#892).

Tracks when the per-connector KMC write key expires so operators can
rotate before expiry. NULL = non-expiring (legacy rows provisioned before
this migration). The rotate endpoint sets this to now + configurable TTL.
"""

import sqlalchemy as sa
from alembic import op

revision = "e33_892_kmc_key_expiry"
down_revision = "e32_886_delivery_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_connectors",
        sa.Column("kmc_api_key_expires_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspace_connectors", "kmc_api_key_expires_at")
