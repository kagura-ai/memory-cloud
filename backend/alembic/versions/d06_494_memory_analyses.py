"""Add Memory Broadlistening schema (#494, umbrella #493).

Issue #494 (B1 of the Broadlistening v1 atomic split): the persistence
foundation for the kouchou-ai-style clustering analyses introduced in
the v0.15.0 cycle. All later phases (B2 #495 pipeline, B3 #496 API+MCP,
F4 #497 frontend) depend on these tables existing.

This migration is intentionally atomic: workspace ALTERs, the three new
tables, and the WorkspaceAddon CHECK extension that registers the new
``extra_analysis_runs`` Stripe SKU all ship together. Splitting them
would leave the addon column without a matching CHECK extension — a
Stripe webhook trying to insert ``extra_analysis_runs`` between the
two PRs would IntegrityError on the existing constraint.

Schema additions:

1. ``workspaces`` — three new columns:

   - ``analysis_default_model_id BIGINT REFERENCES llm_pricing(id)
     ON DELETE SET NULL`` (nullable). Per-workspace default model for
     run-time analyses. SET NULL on pricing-row deletion so a future
     pricing cleanup never cascade-deletes a workspace.
   - ``analysis_quality_model_id BIGINT REFERENCES llm_pricing(id)
     ON DELETE SET NULL`` (nullable). Optional higher-quality variant
     for runs that opt into it (UI selection in #497).
   - ``addon_analysis_bonus INTEGER NOT NULL DEFAULT 0``. Mirrors the
     six other ``addon_*_bonus`` columns. ``server_default="0"`` makes
     this a metadata-only catalog change on PG ≥ 11 — no rewrite.

2. ``memory_analyses`` — one row per run. Frozen ``model_snapshot``
   (JSONB) keeps cost reports reproducible across pricing changes.
   ``status`` and ``paid_by`` carry both Python-side ``default=`` and
   ``server_default`` (dual-default pattern, see ``models/sleep.py``)
   so flush() and raw INSERT both satisfy NOT NULL.

3. ``memory_analysis_clusters`` — flat cluster set per run.
   ``parent_id`` is nullable on day 1 so the v2 hierarchical layer
   lands without another migration.

4. ``memory_analysis_assignments`` — composite PK
   ``(analysis_id, memory_id)``: a memory can appear in many runs, but
   exactly once per run.

5. ``workspace_addons.check_addon_type`` CHECK rewrite — extends the
   IN list with ``'extra_analysis_runs'``. PostgreSQL has no
   ALTER CONSTRAINT for CHECK clauses; drop + recreate is the
   established pattern (b05_223 precedent). For the small populated
   ``workspace_addons`` table the brief ACCESS EXCLUSIVE during
   recreate is acceptable — no zero-downtime two-step needed here.

Revision ID: d06_494_memory_analyses
Revises: d05_523_source_paid_by

NOTE: Revision ID is 23 chars (alembic_version.version_num is
VARCHAR(32) — asyncpg raises StringDataRightTruncationError otherwise).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d06_494_memory_analyses"
down_revision: str | Sequence[str] | None = "d05_523_source_paid_by"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Derive the new IN-list from the old one so the delta is unambiguous —
# adding another SKU later means appending to the new tuple, no
# hand-editing of two parallel string literals. Frozen here (not
# imported from analysis.py) because alembic migrations must remain
# stable under future model edits.
_OLD_ADDON_TYPES: tuple[str, ...] = (
    "extra_storage",
    "extra_memory",
    "extra_mcp_quota",
    "extra_rest_quota",
    "extra_public_quota",
    "extra_members",
    "extra_contexts",
)
_NEW_ADDON_TYPES: tuple[str, ...] = _OLD_ADDON_TYPES + ("extra_analysis_runs",)


def _addon_check_sql(types: tuple[str, ...]) -> str:
    return f"addon_type IN ({', '.join(repr(t) for t in types)})"


_OLD_ADDON_TYPES_SQL = _addon_check_sql(_OLD_ADDON_TYPES)
_NEW_ADDON_TYPES_SQL = _addon_check_sql(_NEW_ADDON_TYPES)


def upgrade() -> None:
    """Add Memory Broadlistening schema."""
    # 1. workspaces ALTERs — three new columns. All metadata-only on PG ≥ 11
    #    (NOT NULL + server_default for the integer; nullable for the FKs).
    op.add_column(
        "workspaces",
        sa.Column(
            "analysis_default_model_id",
            sa.BigInteger,
            sa.ForeignKey("llm_pricing.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "analysis_quality_model_id",
            sa.BigInteger,
            sa.ForeignKey("llm_pricing.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "addon_analysis_bonus",
            sa.Integer,
            nullable=False,
            # Bare ``"0"`` matches the existing ``addon_*_bonus`` columns in
            # auth.py:1127-1132 so the autogenerate diff stays clean.
            server_default="0",
        ),
    )

    # 2. memory_analyses — one row per run.
    op.create_table(
        "memory_analyses",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "context_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        # ``triggered_by`` follows the codebase convention for user_id columns
        # — String(255), no FK, mirroring ``sleep_reports.user_id`` and
        # documented in the c01_360 migration.
        sa.Column("triggered_by", sa.String(255), nullable=False),
        sa.Column(
            "model_id",
            sa.BigInteger,
            sa.ForeignKey("llm_pricing.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("model_snapshot", JSONB, nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("input_count", sa.Integer, nullable=False),
        sa.Column("cost_estimated_cents", sa.Integer, nullable=True),
        sa.Column("cost_actual_cents", sa.Integer, nullable=True),
        sa.Column(
            "paid_by",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'byok'"),
        ),
        sa.Column("quality", JSONB, nullable=True),
        sa.Column("overview", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="valid_memory_analysis_status",
        ),
        sa.CheckConstraint(
            "paid_by IN ('byok', 'platform')",
            name="valid_memory_analysis_paid_by",
        ),
    )
    op.create_index(
        "idx_memory_analyses_workspace_started",
        "memory_analyses",
        ["workspace_id", "started_at"],
    )
    op.create_index(
        "idx_memory_analyses_context_started",
        "memory_analyses",
        ["context_id", "started_at"],
    )

    # 3. memory_analysis_clusters — flat cluster rows; parent_id nullable
    #    for the future v2 hierarchical layer (writes NULL on v1).
    op.create_table(
        "memory_analysis_clusters",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "analysis_id",
            UUID(as_uuid=True),
            sa.ForeignKey("memory_analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("memory_analysis_clusters.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("cluster_index", sa.Integer, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("count", sa.Integer, nullable=False),
        # ``centroid_2d`` is a length-2 float array; PostgreSQL doesn't enforce
        # the size constraint and a CHECK on every insert isn't worth the cost.
        # The pipeline (#495) is responsible for emitting exactly two values.
        sa.Column("centroid_2d", ARRAY(sa.Float), nullable=False),
        sa.Column(
            "representative_memory_ids",
            ARRAY(UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column("property_stats", JSONB, nullable=False),
        sa.Column("label_confidence", sa.Float, nullable=False),
        sa.CheckConstraint(
            "label_confidence >= 0 AND label_confidence <= 1",
            name="valid_memory_analysis_cluster_label_confidence",
        ),
    )
    op.create_index(
        "idx_memory_analysis_clusters_analysis",
        "memory_analysis_clusters",
        ["analysis_id"],
    )

    # 4. memory_analysis_assignments — composite PK (analysis_id, memory_id).
    op.create_table(
        "memory_analysis_assignments",
        sa.Column(
            "analysis_id",
            UUID(as_uuid=True),
            sa.ForeignKey("memory_analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "memory_id",
            UUID(as_uuid=True),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cluster_id",
            UUID(as_uuid=True),
            sa.ForeignKey("memory_analysis_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("x", sa.Float, nullable=False),
        sa.Column("y", sa.Float, nullable=False),
        # Composite PK named by SQLAlchemy default (``<table>_pkey``) so it
        # matches the constraint that ``Base.metadata.create_all`` produces
        # in tests — avoids drift between alembic-applied and test-fixture
        # DBs that referencing the PK name in a future migration would trip.
        sa.PrimaryKeyConstraint("analysis_id", "memory_id"),
    )
    op.create_index(
        "idx_memory_analysis_assignments_cluster",
        "memory_analysis_assignments",
        ["cluster_id"],
    )

    # 5. workspace_addons.check_addon_type — drop + recreate with the new
    #    'extra_analysis_runs' SKU. Mirrors the b05_223 precedent
    #    (valid_edge_type extension) — PostgreSQL has no ALTER CONSTRAINT
    #    for CHECK clauses, drop + recreate is the only path.
    op.drop_constraint(
        "check_addon_type",
        "workspace_addons",
        type_="check",
    )
    op.create_check_constraint(
        "check_addon_type",
        "workspace_addons",
        _NEW_ADDON_TYPES_SQL,
    )


def downgrade() -> None:
    """Drop Memory Broadlistening schema in reverse order.

    Rolling back is destructive: any rows in the three new tables (and
    in any existing ``workspace_addons`` row with
    ``addon_type='extra_analysis_runs'``) will be lost. The CHECK
    constraint recreate at the end of downgrade would otherwise fail
    validation against existing rows, so we delete those rows first —
    same destructive pattern documented in b05_223 for tag_cooccurrence.

    OPS WARNING: ``extra_analysis_runs`` is a Stripe-backed paid SKU, NOT a
    regenerable artifact like b05_223's tag-cooccurrence edges. Deleting
    these rows on rollback severs a paying-customer addon from the
    workspace and the Stripe webhook will NOT re-insert it (subscription
    history is retained Stripe-side, but the DB linkage is lost). Before
    running this downgrade against production:

        SELECT count(*) FROM workspace_addons
         WHERE addon_type = 'extra_analysis_runs';

    If the count is non-zero, do NOT downgrade — coordinate Stripe-side
    cancellation first, or accept that re-applying the migration later
    will leave those workspaces without their paid bonus until manually
    re-inserted.
    """
    # 5'. Restore old CHECK. Delete any rows that the old constraint
    #     would reject so the recreate succeeds.
    op.execute(sa.text("DELETE FROM workspace_addons WHERE addon_type = 'extra_analysis_runs'"))
    op.drop_constraint(
        "check_addon_type",
        "workspace_addons",
        type_="check",
    )
    op.create_check_constraint(
        "check_addon_type",
        "workspace_addons",
        _OLD_ADDON_TYPES_SQL,
    )

    # 4'. assignments → clusters → analyses (reverse FK order).
    op.drop_index(
        "idx_memory_analysis_assignments_cluster",
        table_name="memory_analysis_assignments",
    )
    op.drop_table("memory_analysis_assignments")

    # 3'.
    op.drop_index(
        "idx_memory_analysis_clusters_analysis",
        table_name="memory_analysis_clusters",
    )
    op.drop_table("memory_analysis_clusters")

    # 2'.
    op.drop_index(
        "idx_memory_analyses_context_started",
        table_name="memory_analyses",
    )
    op.drop_index(
        "idx_memory_analyses_workspace_started",
        table_name="memory_analyses",
    )
    op.drop_table("memory_analyses")

    # 1'. workspaces columns.
    op.drop_column("workspaces", "addon_analysis_bonus")
    op.drop_column("workspaces", "analysis_quality_model_id")
    op.drop_column("workspaces", "analysis_default_model_id")
