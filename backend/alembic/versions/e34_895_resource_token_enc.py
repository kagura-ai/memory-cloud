"""Add resource_token_encrypted to workspace_connectors (#895).

Stores the connector's resource token (X-Resource-API-Key) Fernet-encrypted
so the worker config endpoint can return it for the resource-ingest write
path (worker #91 Option A). NULL on legacy rows provisioned before this
migration — the worker falls back to the kmc/remember path for those.
"""

import sqlalchemy as sa
from alembic import op

revision = "e34_895_resource_token_enc"
down_revision = "e33_892_kmc_key_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_connectors",
        sa.Column("resource_token_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspace_connectors", "resource_token_encrypted")
