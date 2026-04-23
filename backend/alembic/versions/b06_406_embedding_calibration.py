"""Add embedding_calibrations table + reset knn_seed_min_similarity=0.4 rows.

Issue #406 (Phase B runtime integration of #240 knn_seed calibration): two
coupled schema changes that let the runtime fallback chain in
``_create_knn_seed_edges`` switch from hardcoded 0.4 to percentile-based
calibration lookup.

1. Create ``embedding_calibrations`` table per #240 D5 schema. Stores a
   5-number summary (p25/p50/p75/p90/p95/p99) of top-k neighbor cosine
   similarity for each ``(model_name, dimensions, context_id)`` combination.
   ``context_id`` is nullable — a NULL row means the calibration is
   model-global (applies to every context using that model + dimensions).
   Per-context rows are allowed by the schema but not populated in v1 (D5
   v2 follow-up — the runtime lookup in ``neural/calibration.py`` has a
   TODO(v2) marker for the per-context path).

   Uniqueness is enforced with two complementary indexes because Postgres
   treats NULL as distinct in a regular UNIQUE constraint — ``(model, dims,
   NULL)`` could otherwise be inserted multiple times:

   - ``uq_calibration_model_dims_nonnull``: UNIQUE on ``(model_name,
     dimensions, context_id)`` where ``context_id IS NOT NULL`` (handles
     the per-context v2 case without collision).
   - ``uq_calibration_model_dims_global``: UNIQUE on ``(model_name,
     dimensions)`` where ``context_id IS NULL`` (at most one global row
     per model+dims).

2. Reset ``knn_seed_min_similarity = 0.4`` rows in ``neural_config``. The
   Python default in ``NeuralMemoryConfig`` changes from ``0.4`` to ``None``
   in the same PR; ``None`` tells the runtime to use the calibration path.
   Operators who tuned the value to anything other than 0.4 (e.g. 0.35,
   0.5) have their explicit rows **preserved** — those rows take priority
   over calibration per D6. The exact value ``0.4`` is deleted because it
   is indistinguishable from the old default and would silently block the
   calibration path.

   This is the known provenance ambiguity flagged in the issue's gate1
   design review: an operator who had explicitly set 0.4 (i.e. confirmed
   the default) loses that setting and falls into the calibration path.
   The CHANGELOG entry for v0.13.1 documents this — operators should
   re-apply their value post-migration if they relied on 0.4 specifically.

Revision ID: b06_406_embedding_calibration
Revises: b05_223_tag_cooccurrence

DOWNGRADE WARNING: the ``knn_seed_min_similarity = 0.4`` row reset is
**not reversible** — we don't know which operators had explicitly set 0.4
vs. inherited the default. Downgrade drops the new table and indexes but
does NOT re-insert deleted rows; callers who need their 0.4 back must
re-apply via admin UI or direct DB update.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "b06_406_embedding_calibration"
down_revision = "b05_223_tag_cooccurrence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create embedding_calibrations + reset knn_seed default rows."""
    # 1. Create embedding_calibrations table.
    #
    # valid_until is NOT given a server_default — the app computes it as
    # sampled_at + NeuralMemoryConfig.calibration_ttl_days (default 30).
    # Keeping the TTL in code config (not hardcoded here) means operators
    # can tighten it via env var without a schema change. See #240 D7.
    op.create_table(
        "embedding_calibrations",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("dimensions", sa.Integer, nullable=False),
        # FK to contexts.id with ON DELETE CASCADE so per-context calibration
        # rows (v2) are auto-cleaned when a context is deleted. Nullable
        # because model-global rows use NULL here (the v1 runtime lookup path).
        # Copilot review PR #420 loop 2 finding.
        sa.Column(
            "context_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("p25", sa.Float, nullable=False),
        sa.Column("p50", sa.Float, nullable=False),
        sa.Column("p75", sa.Float, nullable=False),
        sa.Column("p90", sa.Float, nullable=False),
        sa.Column("p95", sa.Float, nullable=False),
        sa.Column("p99", sa.Float, nullable=False),
        sa.Column("sample_size", sa.Integer, nullable=False),
        sa.Column(
            "sampled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "p25 >= 0.0 AND p25 <= 1.0 AND "
            "p50 >= 0.0 AND p50 <= 1.0 AND "
            "p75 >= 0.0 AND p75 <= 1.0 AND "
            "p90 >= 0.0 AND p90 <= 1.0 AND "
            "p95 >= 0.0 AND p95 <= 1.0 AND "
            "p99 >= 0.0 AND p99 <= 1.0",
            name="embedding_calibrations_percentiles_in_range",
        ),
        sa.CheckConstraint(
            "sample_size >= 0",
            name="embedding_calibrations_nonneg_sample_size",
        ),
        sa.CheckConstraint(
            "valid_until > sampled_at",
            name="embedding_calibrations_valid_until_future",
        ),
    )

    # 2. Two partial-unique indexes so NULL context_id = "at most one global
    #    row per (model, dims)" while non-NULL context_id allows one row per
    #    (model, dims, context_id).
    op.create_index(
        "uq_calibration_model_dims_global",
        "embedding_calibrations",
        ["model_name", "dimensions"],
        unique=True,
        postgresql_where=sa.text("context_id IS NULL"),
    )
    op.create_index(
        "uq_calibration_model_dims_nonnull",
        "embedding_calibrations",
        ["model_name", "dimensions", "context_id"],
        unique=True,
        postgresql_where=sa.text("context_id IS NOT NULL"),
    )

    # 3. Reset knn_seed_min_similarity=0.4 rows in neural_config. The Python
    #    default flips from 0.4 to None in the same PR; deleting the row lets
    #    the code default take over. Rows with other values (operator tuning)
    #    are preserved — they win over calibration per D6.
    #
    #    CAST to float handles operators who stored "0.40" or "0.4000" (value
    #    column is varchar). We compare numerically to catch all equivalent
    #    string forms of 0.4.
    op.execute(
        """
        DELETE FROM neural_config
        WHERE key = 'knn_seed_min_similarity'
          AND value_type = 'float'
          AND CAST(value AS FLOAT) = 0.4
        """
    )


def downgrade() -> None:
    """Drop the new table + indexes.

    Non-reversible: the ``knn_seed_min_similarity = 0.4`` row DELETE cannot
    be undone because post-migration state does not record which operators
    had explicitly set 0.4 vs. inherited the default. Operators needing
    0.4 back must re-apply it after downgrade (admin UI or direct
    ``INSERT INTO neural_config``).
    """
    op.drop_index(
        "uq_calibration_model_dims_nonnull",
        table_name="embedding_calibrations",
    )
    op.drop_index(
        "uq_calibration_model_dims_global",
        table_name="embedding_calibrations",
    )
    op.drop_table("embedding_calibrations")
