"""Real Alembic upgrade/downgrade for the public MCP usage event tables (#2678)."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

PRIOR = "f27_2675_public_mcp_endpoints"
REVISION = "f28_2678_public_mcp_usage_events"
TABLES = ("public_mcp_usage_events", "public_mcp_doc_keys", "public_mcp_usage_retention")

_TOOL_CALL = (
    "INSERT INTO public_mcp_usage_events (occurred_at, kind, endpoint_id, workspace_id, "
    "context_id, resource_pk, resource_id, tool, outcome, era, duration_ms) "
    "VALUES (now(), 'tool_call', :e, :w, :c, :r, 'catalog', 'recall', 'success', "
    "'session', 12) RETURNING id, txid, event_id"
)


def test_f28_creates_usage_tables_and_downgrades_cleanly():
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(PRIOR)
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            for table in TABLES:
                assert conn.execute(
                    text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
                ).scalar()
            indexes = {
                row.indexname: row.indexdef
                for row in conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'public_mcp_usage_events'"
                    )
                ).all()
            }
            assert "(txid, id)" in indexes["ix_pmue_txid_id"]
            assert "(occurred_at)" in indexes["ix_pmue_occurred_at"]
            assert "endpoint_id IS NOT NULL" in indexes["ix_pmue_endpoint_occurred"]
            assert "public_mcp_usage_events_event_id_key" in indexes
            # The events table has no foreign keys (rows outlive endpoints).
            fk_count = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.table_constraints "
                    "WHERE table_name = 'public_mcp_usage_events' "
                    "AND constraint_type = 'FOREIGN KEY'"
                )
            ).scalar()
            assert fk_count == 0
            # The singleton purge mark exists at (0, 0).
            mark = conn.execute(
                text(
                    "SELECT id, purged_through_txid, purged_through_id, purged_total "
                    "FROM public_mcp_usage_retention"
                )
            ).all()
            assert [tuple(r) for r in mark] == [(1, 0, 0, 0)]

        ids = {"e": uuid4(), "w": uuid4(), "c": uuid4(), "r": uuid4()}
        with engine.begin() as conn:
            row = conn.execute(text(_TOOL_CALL), ids).one()
            xact = conn.execute(text("SELECT pg_current_xact_id()::text::bigint")).scalar()
            # The server default stamps the inserting transaction id.
            assert row.txid == xact
            assert row.event_id is not None
            conn.execute(
                text(
                    "INSERT INTO public_mcp_usage_events (occurred_at, kind, dropped_count, "
                    "gap_started_at, gap_ended_at, gap_reason) "
                    "VALUES (now(), 'gap', 3, now(), now(), 'queue_full')"
                )
            )

        # A tool_call row without identity is refused.
        with pytest.raises(IntegrityError, match="pmue_tool_call_shape"), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO public_mcp_usage_events (occurred_at, kind, tool, outcome, "
                    "era, duration_ms) VALUES (now(), 'tool_call', 'recall', 'success', "
                    "'session', 1)"
                )
            )
        # A gap row without counts is refused.
        with pytest.raises(IntegrityError, match="pmue_gap_shape"), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO public_mcp_usage_events (occurred_at, kind, gap_reason) "
                    "VALUES (now(), 'gap', 'shutdown')"
                )
            )
        # An unknown outcome is refused.
        with pytest.raises(IntegrityError, match="valid_pmue_outcome"), engine.begin() as conn:
            conn.execute(
                text(_TOOL_CALL.replace("'success'", "'teapot'")),
                ids,
            )
        # An oversize documents array is refused.
        with pytest.raises(IntegrityError, match="pmue_documents_size"), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO public_mcp_usage_events (occurred_at, kind, endpoint_id, "
                    "workspace_id, context_id, resource_pk, resource_id, tool, outcome, era, "
                    "duration_ms, documents) VALUES (now(), 'tool_call', :e, :w, :c, :r, "
                    "'catalog', 'recall', 'success', 'session', 1, "
                    "to_jsonb(repeat('x', 5000)))"
                ),
                ids,
            )
        # A second singleton row is refused (PK), and so is any other id (CHECK).
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("INSERT INTO public_mcp_usage_retention (id) VALUES (1)"))
        with pytest.raises(IntegrityError, match="pmur_singleton"), engine.begin() as conn:
            conn.execute(text("INSERT INTO public_mcp_usage_retention (id) VALUES (2)"))

        _downgrade_to(PRIOR)
        with engine.connect() as conn:
            for table in TABLES:
                assert (
                    conn.execute(text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}).scalar()
                    is None
                )
    finally:
        engine.dispose()
        _reset_alembic_state()
        _upgrade_to("head")
