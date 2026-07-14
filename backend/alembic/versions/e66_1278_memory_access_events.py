"""Add memory_access_events — append-only agent memory-access audit (RFC-0002 P0-5, #1278).

Pure additive migration (blue-green safe): a new table + indexes + append-only
triggers. CHECK literals are byte-identical to the module-level tuples in
``models/memory_access_event.py`` (drift-pinned by
tests/test_memory_access_event_constants.py).

Append-only enforcement mirrors the ``e50_1128`` secret-store precedent: a
``BEFORE UPDATE OR DELETE`` row trigger + a ``BEFORE TRUNCATE`` statement
trigger, sharing one guard function. UNLIKE e50, UPDATE is permitted when it
changes ONLY the erasure carve-out columns (user_id, session_id, run_id,
event_metadata) — GDPR/APPI pseudonymize + scrub. The guard compares the
immutable column subset (row-as-jsonb minus the carve-out keys) OLD vs NEW.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e66_1278_mae"
down_revision = "e65_1275_api_keys_agent"
branch_labels = None
depends_on = None

_CARVE_OUT = "'user_id','session_id','run_id','event_metadata'"


def upgrade() -> None:
    op.create_table(
        "memory_access_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("principal_type", sa.String(length=16), nullable=False),
        sa.Column("api_key_prefix", sa.String(length=16), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("run_id", sa.String(length=128), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column("span_id", sa.String(length=16), nullable=True),
        sa.Column("surface", sa.String(length=10), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("policy_decision", sa.String(length=20), nullable=True),
        sa.Column("policy_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("query_hash", sa.String(length=64), nullable=True),
        sa.Column("event_metadata", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
            "'load_pinned', 'bootstrap', 'feedback')",
            name="valid_mae_operation",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'error', 'partial')", name="valid_mae_outcome"
        ),
        sa.CheckConstraint("surface IN ('mcp', 'rest')", name="valid_mae_surface"),
        sa.CheckConstraint(
            "principal_type IN ('api_key', 'oauth', 'session')", name="valid_mae_principal"
        ),
        sa.CheckConstraint(
            "policy_decision IS NULL OR policy_decision IN ('allowed', 'binding_denied', "
            "'rbac_denied', 'would_deny', 'unbound')",
            name="valid_mae_policy",
        ),
        sa.CheckConstraint("octet_length(event_metadata::text) <= 4096", name="mae_metadata_size"),
    )
    op.create_index("idx_mae_occurred", "memory_access_events", ["occurred_at"])
    op.create_index(
        "idx_mae_workspace_occurred", "memory_access_events", ["workspace_id", "occurred_at"]
    )
    op.create_index(
        "idx_mae_agent_occurred",
        "memory_access_events",
        ["agent_id", "occurred_at"],
        postgresql_where=sa.text("agent_id IS NOT NULL"),
    )

    # Append-only guard with the erasure carve-out. On UPDATE, everything
    # OUTSIDE the carve-out must be unchanged; DELETE and TRUNCATE always raise.
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION memory_access_events_append_only()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    IF (to_jsonb(OLD) - ARRAY[{_CARVE_OUT}])
                       IS DISTINCT FROM
                       (to_jsonb(NEW) - ARRAY[{_CARVE_OUT}]) THEN
                        RAISE EXCEPTION
                            'memory_access_events is append-only; only '
                            '(user_id, session_id, run_id, event_metadata) may be '
                            'updated (erasure carve-out)'
                            USING ERRCODE = 'restrict_violation';
                    END IF;
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION
                    'memory_access_events is append-only; % is not permitted', TG_OP
                    USING ERRCODE = 'restrict_violation';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER memory_access_events_no_mutate
            BEFORE UPDATE OR DELETE ON memory_access_events
            FOR EACH ROW EXECUTE FUNCTION memory_access_events_append_only();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER memory_access_events_no_truncate
            BEFORE TRUNCATE ON memory_access_events
            FOR EACH STATEMENT EXECUTE FUNCTION memory_access_events_append_only();
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS memory_access_events_no_truncate ON memory_access_events")
    )
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS memory_access_events_no_mutate ON memory_access_events")
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS memory_access_events_append_only()"))
    op.drop_index("idx_mae_agent_occurred", table_name="memory_access_events")
    op.drop_index("idx_mae_workspace_occurred", table_name="memory_access_events")
    op.drop_index("idx_mae_occurred", table_name="memory_access_events")
    op.drop_table("memory_access_events")
