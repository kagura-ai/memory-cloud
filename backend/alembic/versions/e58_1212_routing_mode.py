"""#1212: add context_search_configs.routing_mode (query-intent router).

Adds the experiment gate for the query-intent retrieval router:
``routing_mode`` in ('off', 'log_only', 'active'), server_default 'off'.
Existing rows and new rows both land on 'off' — zero behavior change at
migration time; the router only runs on contexts an operator opts in.

MIGRATION COORDINATION (v0.45.0): e55 (#1215), e56 (#1217), e57 (#1218) and
this e58 ALL chain from e54 on their respective unmerged branches. Whichever
PR merges later MUST bump ``down_revision`` to the then-current head (e.g. if
e55/e56/e57 merge first, this becomes ``down_revision = "e57_1208_..."``).
Alembic fails loudly on multiple heads, so a miss cannot ship silently.

Blue-green safety: ADD COLUMN with a server_default is metadata-only on
PG 11+ (no table rewrite); old app code never references the column.

Revision ID: e58_1212_routing_mode
Revises: e54_1183_sleep_status_degraded
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "e58_1212_routing_mode"
down_revision = "e54_1183_sleep_status_degraded"
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
