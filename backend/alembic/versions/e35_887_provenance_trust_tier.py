"""Server-authoritative provenance + context trust_tier (#887).

Two changes, both additive/safe:

1. ``memories.source_type`` → NOT NULL + CHECK. Legacy NULL rows are backfilled
   to 'manual' (the ratified, CSO-signed-off rule — safe because trust is
   authoritative at the context level, not per-row, and legacy NULL rows predate
   connector ingestion). 'connector' is added to the allowed set (server-stamped
   by resource_indexer).
2. ``contexts.trust_tier`` — new NOT NULL column (default 'trusted') + CHECK.
   Connector contexts are stamped 'external' at provision time; a
   ``recall(filters={'trust_tier':'trusted'})`` read excludes external-origin
   contexts from behaviour-influencing reads.

NOTE: shares down_revision e34 with the #889 lane migration (PR #905). If both
land, a one-line alembic merge revision reconciles the two heads (or rebase
whichever merges second onto the other).
"""

from alembic import op

revision = "e35_887_provenance_trust_tier"
down_revision = "e34_895_resource_token_enc"
branch_labels = None
depends_on = None

# Byte-identical to the model CHECK literals (memory._ALL_SOURCE_TYPES /
# auth._ALL_CONTEXT_TRUST_TIERS, repr-derived). Pinned by tests.
_SOURCE_TYPE_CHECK = "source_type IN ('file', 'url', 'vault', 'api', 'manual', 'connector')"
_TRUST_TIER_CHECK = "trust_tier IN ('trusted', 'external')"


def upgrade() -> None:
    # 1. memories.source_type → backfill, NOT NULL, default, CHECK.
    op.execute("UPDATE memories SET source_type = 'manual' WHERE source_type IS NULL")
    op.alter_column(
        "memories",
        "source_type",
        nullable=False,
        server_default="manual",
    )
    op.create_check_constraint("valid_source_type", "memories", _SOURCE_TYPE_CHECK)

    # 2. contexts.trust_tier → new NOT NULL column (default 'trusted') + CHECK.
    op.execute("ALTER TABLE contexts ADD COLUMN trust_tier VARCHAR(20) NOT NULL DEFAULT 'trusted'")
    op.create_check_constraint("valid_context_trust_tier", "contexts", _TRUST_TIER_CHECK)


def downgrade() -> None:
    op.drop_constraint("valid_context_trust_tier", "contexts", type_="check")
    op.drop_column("contexts", "trust_tier")

    op.drop_constraint("valid_source_type", "memories", type_="check")
    op.alter_column(
        "memories",
        "source_type",
        nullable=True,
        server_default=None,
    )
