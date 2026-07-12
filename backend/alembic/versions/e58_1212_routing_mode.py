"""#1212: add context_search_configs.routing_mode (query-intent router).

Adds the experiment gate for the query-intent retrieval router:
``routing_mode`` in ('off', 'log_only', 'active'), server_default 'off'.
Existing rows and new rows both land on 'off' — zero behavior change at
migration time; the router only runs on contexts an operator opts in.

MIGRATION COORDINATION (v0.45.0): e55 (#1215), e56 (#1217), e57 (#1218) and
this e58 ALL chained from e54 on their respective branches. e55/e56/e57
merged first, so this revision was re-chained onto e57 (the then-current
head) at merge time, per the plan in epic #1214.

Blue-green safety: ADD COLUMN with a server_default is metadata-only on
PG 11+ (no table rewrite); old app code never references the column.

Revision ID: e58_1212_routing_mode
Revises: e57_1208_supersedes_contradicts
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "e58_1212_routing_mode"
down_revision = "e57_1208_supersedes_contradicts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add routing_mode with CHECK constraint, default 'off'."""
    op.add_column(
        "context_search_configs",
        sa.Column("routing_mode", sa.String(10), nullable=False, server_default="off"),
    )
    op.create_check_constraint(
        "routing_mode_check",
        "context_search_configs",
        "routing_mode IN ('off', 'log_only', 'active')",
    )


def downgrade() -> None:
    """Drop the routing_mode column and its CHECK constraint."""
    op.drop_constraint("routing_mode_check", "context_search_configs", type_="check")
    op.drop_column("context_search_configs", "routing_mode")
