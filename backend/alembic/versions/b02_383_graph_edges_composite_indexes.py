"""Add (workspace_id, context_id) composite indexes on neural_memory_edges (#383).

Issue #383: graph reads move from ``user_id == caller`` hardcoded filter to
``PermissionService``-driven visibility. The new primary access pattern is
``WHERE workspace_id = :ws AND context_id = :ctx (AND src_id|dst_id = :node)``
— the existing ``idx_edges_user_src`` / ``idx_edges_user_dst`` composite
indexes are ``user_id``-leading and therefore inert for shared-context reads
under PostgreSQL's leftmost-prefix rule.

This migration adds two composite indexes matching the new access pattern:
``(workspace_id, context_id, src_id)`` and ``(workspace_id, context_id, dst_id)``.

Old indexes are intentionally **retained** in this revision — they still
serve the private-context read path (caller-scoped edges) and internal
admin paths (decay, consolidation) that filter by ``user_id``. A follow-up
migration may remove them once production query plans confirm they are
unused.

Revision ID: b02_383_edges_ws_ctx_idx
Revises: b01_resource_pk_ph2
Create Date: 2026-04-20
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b02_383_edges_ws_ctx_idx"
down_revision: str | Sequence[str] | None = "b01_resource_pk_ph2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create composite indexes for the new shared-context graph read path."""
    op.create_index(
        "idx_edges_ws_ctx_src",
        "neural_memory_edges",
        ["workspace_id", "context_id", "src_id"],
        unique=False,
    )
    op.create_index(
        "idx_edges_ws_ctx_dst",
        "neural_memory_edges",
        ["workspace_id", "context_id", "dst_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the composite indexes added in ``upgrade``."""
    op.drop_index("idx_edges_ws_ctx_dst", table_name="neural_memory_edges")
    op.drop_index("idx_edges_ws_ctx_src", table_name="neural_memory_edges")
