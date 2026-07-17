"""Add WHERE-axis generated columns location_lat / location_lon + partial index (#1331).

Mirrors e30_877_time_trigger_cols for the spatial axis. Unlike the time
columns' TEXT trick (::timestamp is only STABLE), ``::double precision`` is
IMMUTABLE, so these are real numeric STORED generated columns. The regex
guard NULLs malformed details.location values instead of failing the INSERT
(raw-SQL defense); MemoryService._apply_location's normalize_location is the
writer-side contract (validation + 7-decimal fixed-point write-back) and
JSONB renders numerics via ``numeric`` (never exponent notation), so
well-formed writes always match the guard.

NOTE: adding STORED generated columns rewrites the table — check the prod
``memories`` row count before applying (e30_877 was the same shape and
passed production).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "e68_1331_location_cols"
down_revision = "e67_1281_agent_ws"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE memories
        ADD COLUMN location_lat DOUBLE PRECISION
        GENERATED ALWAYS AS (CASE WHEN details->'location'->>'lat' ~ '^-?[0-9]+(\.[0-9]+)?$'
        THEN (details->'location'->>'lat')::double precision ELSE NULL END) STORED
        """
    )
    op.execute(
        r"""
        ALTER TABLE memories
        ADD COLUMN location_lon DOUBLE PRECISION
        GENERATED ALWAYS AS (CASE WHEN details->'location'->>'lon' ~ '^-?[0-9]+(\.[0-9]+)?$'
        THEN (details->'location'->>'lon')::double precision ELSE NULL END) STORED
        """
    )
    # Partial btree backing recall_nearby's bbox prefilter. The predicate
    # carries deleted_at IS NULL (an improvement over idx_memories_trigger_from)
    # — the query must repeat both conditions verbatim for the index to serve.
    op.create_index(
        "idx_memories_location",
        "memories",
        ["location_lat", "location_lon"],
        postgresql_where=sa.text("location_lat IS NOT NULL AND deleted_at IS NULL"),
    )
    # Range CHECK — raw-SQL defense behind normalize_location. A CHECK passes
    # when the expression is NULL; the explicit IS NULL arms make the
    # "no location = pass" reading unmissable while a present-but-out-of-range
    # value is a definite FALSE.
    op.create_check_constraint(
        "valid_location_range",
        "memories",
        "(location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)) "
        "AND (location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180))",
    )


def downgrade() -> None:
    op.drop_constraint("valid_location_range", "memories", type_="check")
    op.drop_index("idx_memories_location", table_name="memories")
    op.drop_column("memories", "location_lon")
    op.drop_column("memories", "location_lat")
