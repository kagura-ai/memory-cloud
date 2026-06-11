"""Add ``kind`` to embedding_calibrations for edge-gate calibration (#982).

Issue #982 (follow-up to #969): calibrate ``min_similarity_for_edge`` (the
co-activation / Hebbian semantic gate, #118) per embedding model instead of
the absolute 0.5 default that the #969 compounding experiment showed rejects
genuine cross-topic pairs (cosines 0.37-0.40 under text-embedding-3-small).

The edge gate is calibrated against the RANDOM-PAIR cosine distribution, which
is a different population from the top-k neighbor distribution that #406 stores
for knn-seed calibration. Rather than a parallel table, we tag rows with a
``kind`` so a single ``(model_name, dimensions)`` pair can hold one row of each
distribution:

- ``knn_seed``  = top-k neighbor distribution (#406, pre-existing rows)
- ``edge_gate`` = random-pair distribution (this issue)

Changes:

1. Add ``kind VARCHAR(16) NOT NULL DEFAULT 'knn_seed'``. The default backfills
   every pre-#982 row to ``knn_seed``, preserving their meaning — the runtime
   ``resolve_knn_threshold`` now filters ``kind = 'knn_seed'``.
2. Swap both partial-unique indexes to include ``kind`` so knn_seed and
   edge_gate rows coexist for the same ``(model, dims[, context_id])``.

Revision ID: e38_982_edge_gate_kind
Revises: e37_517_user_oauth_providers

DOWNGRADE WARNING: downgrade DELETEs all ``edge_gate`` rows before restoring the
``kind``-less unique indexes (otherwise a knn_seed + edge_gate pair for the same
(model, dims, NULL) would violate uniqueness). Those rows are recomputable by
the calibration job, so the loss is benign.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e38_982_edge_gate_kind"
down_revision = "e37_517_user_oauth_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``kind`` (backfilled to knn_seed) and widen the unique indexes."""
    # 1. Add kind. NOT NULL + server_default backfills existing rows in one
    #    statement (Postgres applies the default to pre-existing rows).
    op.add_column(
        "embedding_calibrations",
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            server_default="knn_seed",
        ),
    )

    # 2. Swap both partial-unique indexes to include kind so each (model, dims)
    #    can hold one knn_seed row and one edge_gate row.
    op.drop_index(
        "uq_calibration_model_dims_global",
        table_name="embedding_calibrations",
    )
    op.drop_index(
        "uq_calibration_model_dims_nonnull",
        table_name="embedding_calibrations",
    )
    op.create_index(
        "uq_calibration_model_dims_global",
        "embedding_calibrations",
        ["model_name", "dimensions", "kind"],
        unique=True,
        postgresql_where=sa.text("context_id IS NULL"),
    )
    op.create_index(
        "uq_calibration_model_dims_nonnull",
        "embedding_calibrations",
        ["model_name", "dimensions", "context_id", "kind"],
        unique=True,
        postgresql_where=sa.text("context_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Restore the kind-less indexes and drop the column.

    Deletes ``edge_gate`` rows first so the restored ``(model, dims)`` global
    uniqueness (which ignores kind) cannot be violated by a knn_seed/edge_gate
    pair. Those rows are recomputable by the calibration job.
    """
    op.execute("DELETE FROM embedding_calibrations WHERE kind = 'edge_gate'")

    op.drop_index(
        "uq_calibration_model_dims_nonnull",
        table_name="embedding_calibrations",
    )
    op.drop_index(
        "uq_calibration_model_dims_global",
        table_name="embedding_calibrations",
    )
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
    op.drop_column("embedding_calibrations", "kind")
