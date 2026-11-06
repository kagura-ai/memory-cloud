"""Public MCP usage events, doc-key map and retention mark (#2678).

Purely additive: three new tables, plain ``create_table``s, no NOT VALID /
VALIDATE, no CONCURRENTLY. CHECK literals are byte-identical to the tuples in
``models/public_mcp_usage.py`` (pinned by ``tests/test_public_mcp_usage_constants.py``
and ``tests/test_schema_drift.py``). The singleton retention row is inserted
with ``ON CONFLICT DO NOTHING`` so the step is re-runnable.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f28_2678_public_mcp_usage_events"
down_revision = "f27_2675_public_mcp_endpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_mcp_usage_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "event_id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "txid",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("pg_current_xact_id()::text::bigint"),
        ),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("context_id", sa.Uuid(), nullable=True),
        sa.Column("resource_pk", sa.Uuid(), nullable=True),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("tool", sa.String(32), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("era", sa.String(10), nullable=True),
        sa.Column("protocol_version", sa.String(16), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=True),
        sa.Column("continuation", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("documents", postgresql.JSONB(), nullable=True),
        sa.Column(
            "documents_truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("session_key", sa.CHAR(32), nullable=True),
        sa.Column("client_family", sa.String(32), nullable=True),
        sa.Column("dropped_count", sa.Integer(), nullable=True),
        sa.Column("gap_started_at", sa.DateTime(), nullable=True),
        sa.Column("gap_ended_at", sa.DateTime(), nullable=True),
        sa.Column("gap_reason", sa.String(16), nullable=True),
        sa.UniqueConstraint("event_id", name="public_mcp_usage_events_event_id_key"),
        sa.CheckConstraint("kind IN ('tool_call', 'gap')", name="valid_pmue_kind"),
        sa.CheckConstraint(
            "tool IS NULL OR tool IN ('recall', 'reference', 'explore', 'list_tags', "
            "'get_context_info', 'other')",
            name="valid_pmue_tool",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('success', 'degraded', 'denied', 'rate_limited', "
            "'invalid_request', 'error', 'cancelled')",
            name="valid_pmue_outcome",
        ),
        sa.CheckConstraint("era IS NULL OR era IN ('session', 'stateless')", name="valid_pmue_era"),
        sa.CheckConstraint(
            "gap_reason IS NULL OR gap_reason IN ('queue_full', 'write_failed', 'shutdown')",
            name="valid_pmue_gap_reason",
        ),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="pmue_duration_ms"),
        sa.CheckConstraint(
            "dropped_count IS NULL OR dropped_count >= 1", name="pmue_dropped_count"
        ),
        sa.CheckConstraint(
            "documents IS NULL OR octet_length(documents::text) <= 4096",
            name="pmue_documents_size",
        ),
        sa.CheckConstraint(
            "kind <> 'tool_call' OR (endpoint_id IS NOT NULL AND workspace_id IS NOT NULL "
            "AND context_id IS NOT NULL AND resource_pk IS NOT NULL AND resource_id IS NOT NULL "
            "AND tool IS NOT NULL AND outcome IS NOT NULL AND era IS NOT NULL "
            "AND duration_ms IS NOT NULL)",
            name="pmue_tool_call_shape",
        ),
        sa.CheckConstraint(
            "kind <> 'gap' OR (dropped_count IS NOT NULL AND gap_started_at IS NOT NULL "
            "AND gap_ended_at IS NOT NULL AND gap_reason IS NOT NULL)",
            name="pmue_gap_shape",
        ),
    )
    op.create_index("ix_pmue_txid_id", "public_mcp_usage_events", ["txid", "id"])
    op.create_index("ix_pmue_occurred_at", "public_mcp_usage_events", ["occurred_at"])
    op.create_index(
        "ix_pmue_endpoint_occurred",
        "public_mcp_usage_events",
        ["endpoint_id", "occurred_at"],
        postgresql_where=sa.text("endpoint_id IS NOT NULL"),
    )

    op.create_table(
        "public_mcp_doc_keys",
        sa.Column(
            "resource_pk",
            sa.Uuid(),
            sa.ForeignKey(
                "resources.id", ondelete="CASCADE", name="fk_public_mcp_doc_keys_resource_pk"
            ),
            nullable=False,
        ),
        sa.Column("doc_key", sa.CHAR(32), nullable=False),
        sa.Column("resource_doc_id", sa.String(255), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("resource_pk", "doc_key", name="public_mcp_doc_keys_pkey"),
    )

    op.create_table(
        "public_mcp_usage_retention",
        sa.Column("id", sa.SmallInteger(), primary_key=True, autoincrement=False),
        sa.Column("purged_through_txid", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("purged_through_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("purged_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("purged_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("id = 1", name="pmur_singleton"),
    )
    op.execute(
        sa.text("INSERT INTO public_mcp_usage_retention (id) VALUES (1) ON CONFLICT DO NOTHING")
    )


def downgrade() -> None:
    op.drop_table("public_mcp_usage_retention")
    op.drop_table("public_mcp_doc_keys")
    op.drop_table("public_mcp_usage_events")
