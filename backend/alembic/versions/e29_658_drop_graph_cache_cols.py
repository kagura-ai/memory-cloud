"""Drop the dead ``graph_memory`` cache columns (#658).

Issue #658 (follow-up to PR #656 / #651): the four statistics columns on
``graph_memory`` —

- ``total_nodes``
- ``total_edges``
- ``avg_edge_weight``
- ``max_edge_weight``

— were a per-user cache that PR #656 stopped maintaining. ``weight_decay_task``
used to refresh them every hour, but that write was removed because it tripped
the 3-level isolation validator and the cache semantics (per-user, aggregated
across workspaces/contexts) are incompatible with PR #394's invariant
(``get_stats`` explicitly forbids cross-tenant aggregation). A grep confirmed
**zero readers**: the live ``/graph/stats`` surface and the frontend compute
node/edge/weight stats on the fly from ``graph_data`` via
``GraphService.stats()`` — they never read these columns. With #656 merged the
columns are write-free as well, so they are fully dead and removed here.

### Downgrade

``downgrade()`` re-adds the four columns as ``nullable=False`` with
``server_default 0 / 0.0``. Note this server_default is *not* a restoration of
the baseline definition — the baseline (157247e0df86) declared these columns
``nullable=False`` with **no** server_default and relied on the model's
app-layer ``default=0``. The server_default is added here deliberately because
re-adding a ``NOT NULL`` column to an already-populated ``graph_memory`` table
would fail without one. The pre-drop values cannot be reconstructed (a cache
with no surviving source beyond re-deriving from ``graph_data``), but since the
columns were write-only dead cache, restoring them as zeros is the honest
reversible path — old code finds zeros, matching the unmaintained state they
were already in.

Revision ID: e29_658_drop_graph_cache_cols
Revises: e28_850_workspace_connectors
"""

import sqlalchemy as sa

from alembic import op

# NOTE: revision id kept <= 32 chars — alembic_version.version_num is VARCHAR(32).
revision = "e29_658_drop_graph_cache_cols"
down_revision = "e28_850_workspace_connectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("graph_memory", "total_nodes")
    op.drop_column("graph_memory", "total_edges")
    op.drop_column("graph_memory", "avg_edge_weight")
    op.drop_column("graph_memory", "max_edge_weight")


def downgrade() -> None:
    op.add_column(
        "graph_memory",
        sa.Column("total_nodes", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "graph_memory",
        sa.Column("total_edges", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "graph_memory",
        sa.Column("avg_edge_weight", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "graph_memory",
        sa.Column("max_edge_weight", sa.Float(), nullable=False, server_default="0"),
    )
