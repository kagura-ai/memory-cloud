"""Add validated per-connector worker runtime storage (#1348).

The column is nullable so existing connector rows and rolling deployments keep
the pre-#1348 behavior: the worker config endpoint omits ``runtime`` and old or
new workers use their built-in defaults. Validation belongs at the REST/MCP
admin boundary; PostgreSQL stores the normalized JSONB document.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e70_1348_worker_runtime"
down_revision = "e69_1331_location_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_connectors",
        sa.Column("runtime_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspace_connectors", "runtime_config")
