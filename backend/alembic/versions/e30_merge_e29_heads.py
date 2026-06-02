"""Merge e29 heads (drop_graph_cache_cols + memories_ws_ctx_idx).

``e29_658_drop_graph_cache_cols`` and ``e29_619_memories_ws_ctx_idx`` both
branch from ``e28_850_workspace_connectors``.  This empty merge revision
converges them into a single head so ``alembic upgrade head`` succeeds.

No schema changes here — both branches are independently safe.  This is
purely a topology fix, following the e11_merge_e10_heads.py precedent.
"""

from collections.abc import Sequence

revision: str = "e30_merge_e29_heads"
down_revision: str | Sequence[str] | None = (
    "e29_658_drop_graph_cache_cols",
    "e29_619_memories_ws_ctx_idx",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
