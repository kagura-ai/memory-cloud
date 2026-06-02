"""Add Time Memory generated columns trigger_from / trigger_until + partial index.

Mirrors the external_blob_* GENERATED ALWAYS AS ... STORED pattern from
e03_485_file_objects. Columns are TEXT extractions of details.trigger.from/until
(naive ISO strings written by MemoryService.remember). A plain ``->>`` extraction
is IMMUTABLE; a ``::timestamp`` cast is only STABLE (DateStyle-dependent) and
PostgreSQL rejects it in a STORED generated column. The from/until strings are
fixed-width zero-padded ISO, so lexical order == chronological order, which backs
the window-overlap query + ORDER BY trigger_from sort on GET /memory/list.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "e30_877_time_trigger_cols"
down_revision = "e29_619_memories_ws_ctx_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memories
        ADD COLUMN trigger_from VARCHAR(32)
        GENERATED ALWAYS AS (details->'trigger'->>'from') STORED
        """
    )
    op.execute(
        """
        ALTER TABLE memories
        ADD COLUMN trigger_until VARCHAR(32)
        GENERATED ALWAYS AS (details->'trigger'->>'until') STORED
        """
    )
    op.create_index(
        "idx_memories_trigger_from",
        "memories",
        ["trigger_from"],
        postgresql_where=sa.text("type = 'time'"),
    )


def downgrade() -> None:
    op.drop_index("idx_memories_trigger_from", table_name="memories")
    op.drop_column("memories", "trigger_until")
    op.drop_column("memories", "trigger_from")
