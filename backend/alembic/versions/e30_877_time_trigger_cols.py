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
    # Defense-in-depth for the lexical==chronological invariant: a type='time'
    # row MUST carry both window bounds in fixed-width zero-padded ISO. Gated on
    # type<>'time' so non-time memories that happen to have a details.trigger.*
    # path are unaffected. Catches raw/admin SQL or any future write path that
    # bypasses MemoryService.normalize_trigger, where a malformed or NULL bound
    # would silently corrupt ORDER BY / window-overlap results.
    # The IS NOT NULL guards are load-bearing: a CHECK passes when the
    # expression is TRUE *or NULL*, and `NULL ~ regex` is NULL — so without the
    # explicit non-null test a type='time' row missing a bound would slip
    # through. Spelling them out makes the predicate a definite FALSE in that
    # case, enforcing presence as well as format.
    op.create_check_constraint(
        "valid_trigger_window_format",
        "memories",
        "type <> 'time' OR ("
        "trigger_from IS NOT NULL "
        "AND trigger_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$' "
        "AND trigger_until IS NOT NULL "
        "AND trigger_until ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$')",
    )


def downgrade() -> None:
    op.drop_constraint("valid_trigger_window_format", "memories", type_="check")
    op.drop_index("idx_memories_trigger_from", table_name="memories")
    op.drop_column("memories", "trigger_until")
    op.drop_column("memories", "trigger_from")
