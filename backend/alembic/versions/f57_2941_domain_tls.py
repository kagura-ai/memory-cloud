"""Latest edge observation for each public MCP domain claim."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "f57_2941_domain_tls"
down_revision = "f56_2918_workspace_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("public_mcp_domains", sa.Column("tls_report", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("public_mcp_domains", "tls_report")
