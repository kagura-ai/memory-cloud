"""Add tag_cooccurrence edge type, GIN index on memories.tags, hub_tag_cache.

Issue #223 (Tier 2 cold-start seeding): three coupled schema changes that ship
together because the runtime code that uses them is added in the same PR.

1. Drop + recreate ``valid_edge_type`` CHECK constraint to add the new
   ``tag_cooccurrence`` value. Postgres does not support adding values to a
   CHECK constraint in-place; drop + recreate is the canonical pattern.

2. Create ``idx_memories_tags_gin`` — a GIN index on ``memories.tags`` so the
   ``tags && CAST(:query_tags AS varchar[])`` overlap query at remember() time
   can be pushed down to the index instead of degrading to a sequential scan.
   The runtime code casts the *parameter* to ``varchar[]`` (matching the
   column type ``Column(ARRAY(String))`` → ``varchar[]`` in Postgres) so the
   WHERE-clause expression literally is ``tags && X`` and PG can use this
   index. Casting the column instead would form a different expression
   (``tags::text[] && X``) that the GIN index does not match.

   NOTE on CONCURRENTLY: this repo's ``alembic/env.py`` wraps every migration
   in ``context.begin_transaction()``, and Postgres rejects
   ``CREATE INDEX CONCURRENTLY`` inside a transaction block. The async
   ``async_engine_from_config`` path used here also does not plumb
   ``op.get_context().autocommit_block()`` through ``do_run_migrations``.
   We therefore use a plain ``CREATE INDEX``. The ``memories.tags`` column is
   a low-frequency write target (tags are set at remember() and rarely
   updated), so the brief ACCESS EXCLUSIVE lock during build is acceptable.
   If a future deployment needs zero-downtime here, split this into a
   dedicated migration that escapes the env.py transaction wrapping.

3. Create ``hub_tag_cache`` table — per-(workspace, context) cache of "hub
   tags" (tags appearing on >X% of memories in the context). Computed nightly
   by Sleep Maintenance. Mirrors the ``Bm25IdfDriftLog`` shape (FK on
   contexts.id with ON DELETE CASCADE, JSONB payload, server_default
   timestamp).

Revision ID: b05_223_tag_cooccurrence
Revises: b04_358_signup_gate

NOTE: Revision IDs are capped at 32 chars because ``alembic_version.version_num``
is ``VARCHAR(32)`` in this database (asyncpg raises
``StringDataRightTruncationError`` otherwise).

DOWNGRADE WARNING: ``downgrade()`` deletes all ``tag_cooccurrence`` edges
before restoring the original ``valid_edge_type`` CHECK constraint. Without
the delete, constraint recreation would fail validation against the existing
rows. This is destructive — re-applying ``upgrade`` will not recover the
edges; they must be re-seeded by Sleep Maintenance / the backfill script.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "b05_223_tag_cooccurrence"
down_revision = "b04_358_signup_gate"
branch_labels = None
depends_on = None


_OLD_EDGE_TYPES_SQL = (
    "edge_type IN ('neural_association', 'related_to', 'depends_on', "
    "'learned_from', 'semantic_similarity', 'declared_link')"
)
_NEW_EDGE_TYPES_SQL = (
    "edge_type IN ('neural_association', 'related_to', 'depends_on', "
    "'learned_from', 'semantic_similarity', 'declared_link', 'tag_cooccurrence')"
)


def upgrade() -> None:
    """Apply tag_cooccurrence schema changes."""
    # 1. Extend valid_edge_type CHECK constraint to include 'tag_cooccurrence'.
    #    Drop + recreate is the only way; Postgres has no ALTER CONSTRAINT for
    #    CHECK clause modification.
    op.drop_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        type_="check",
    )
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        _NEW_EDGE_TYPES_SQL,
    )

    # 2. GIN index on memories.tags. Required for the tag_cooccurrence seeding
    #    query (`WHERE ... AND tags && ARRAY[?]::text[]`) to scale beyond a
    #    sequential scan.
    op.create_index(
        "idx_memories_tags_gin",
        "memories",
        ["tags"],
        postgresql_using="gin",
    )

    # 3. hub_tag_cache: per-(workspace, context) snapshot of tags considered
    #    "hub" (frequency above tag_cooccurrence_hub_threshold). Refreshed
    #    nightly by Sleep Maintenance; read by remember() to skip hub tags
    #    when computing cooccurrence candidates.
    op.create_table(
        "hub_tag_cache",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "context_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # JSON list of tag strings considered "hub" for this (workspace, context).
        # Empty list ([]) is a valid "no hub tags found this run" result and
        # is distinct from missing row ("never computed"). remember() treats
        # missing as "no exclusion" (graceful first-night behavior).
        sa.Column("hub_tags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        # Total memory count at compute time + threshold used. Useful for
        # debugging "why is X considered hub?" without re-querying memories.
        sa.Column("memory_count", sa.Integer(), nullable=False),
        sa.Column("threshold_used", sa.Float(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # One row per (workspace, context). Refresh = upsert on this key.
        sa.UniqueConstraint(
            "workspace_id",
            "context_id",
            name="uq_hub_tag_cache_ws_ctx",
        ),
        sa.CheckConstraint(
            "memory_count >= 0",
            name="hub_tag_cache_nonneg_memory_count",
        ),
        sa.CheckConstraint(
            "threshold_used >= 0.0 AND threshold_used <= 1.0",
            name="hub_tag_cache_threshold_in_range",
        ),
    )
    op.create_index(
        "ix_hub_tag_cache_workspace_id",
        "hub_tag_cache",
        ["workspace_id"],
    )


def downgrade() -> None:
    """Reverse all three changes in opposite order.

    Destructive: any existing ``tag_cooccurrence`` edges are deleted before
    the CHECK constraint is restored to its pre-#223 form. Without the
    delete, ``op.create_check_constraint`` would validate the existing rows
    against the narrower IN clause and raise. See module docstring.
    """
    op.drop_index("ix_hub_tag_cache_workspace_id", table_name="hub_tag_cache")
    op.drop_table("hub_tag_cache")
    op.drop_index("idx_memories_tags_gin", table_name="memories")
    op.drop_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        type_="check",
    )
    # Drop edges that the about-to-be-restored CHECK would reject. Done after
    # the constraint drop so the DELETE is not blocked by it.
    op.execute("DELETE FROM neural_memory_edges WHERE edge_type = 'tag_cooccurrence'")
    op.create_check_constraint(
        "valid_edge_type",
        "neural_memory_edges",
        _OLD_EDGE_TYPES_SQL,
    )
