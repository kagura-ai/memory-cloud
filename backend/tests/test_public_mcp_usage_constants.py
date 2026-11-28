"""Pins for the public MCP usage event vocabularies (#2678).

The CHECK constraints are rendered from ordered tuples so
``Base.metadata.create_all()`` produces literals byte-identical to the
migration ``f28_2678_public_mcp_usage_events``.
"""

import ast
from pathlib import Path

from sqlalchemy import Index

from models.public_mcp_usage import (
    PMU_CALLER_ANONYMOUS,
    PMU_CALLER_BOUND_KEY,
    PMU_CALLERS,
    PMU_ERAS,
    PMU_GAP_REASONS,
    PMU_KINDS,
    PMU_OUTCOMES,
    PMU_REST_ERA,
    PMU_ROUTE_MCP,
    PMU_ROUTE_REST,
    PMU_ROUTES,
    PMU_SEARCH_TOOL,
    PMU_TOOLS,
    PublicMcpDailyVisitor,
    PublicMcpUsageEvent,
)

_F67 = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "f67_3035_public_visitors.py"


def _check(name: str, model=PublicMcpUsageEvent) -> str:
    c = next(c for c in model.__table_args__ if getattr(c, "name", None) == name)
    return c.sqltext.text


def _f67_literals() -> list[str]:
    tree = ast.parse(_F67.read_text())
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _f67_text() -> str:
    """Every string literal of the migration, adjacent parts joined."""
    return "\n".join(_f67_literals())


def test_vocabularies():
    assert PMU_KINDS == ("tool_call", "gap")
    assert PMU_TOOLS == ("recall", "reference", "explore", "list_tags", "get_context_info", "other")
    assert PMU_OUTCOMES == (
        "success",
        "degraded",
        "denied",
        "rate_limited",
        "invalid_request",
        "error",
        "cancelled",
    )
    assert PMU_ERAS == ("session", "stateless")
    assert PMU_GAP_REASONS == ("queue_full", "write_failed", "shutdown")


def test_outcomes_extend_the_public_hook_vocabulary():
    from mcp_server.public.telemetry import PUBLIC_CALL_OUTCOMES

    assert set(PUBLIC_CALL_OUTCOMES) <= set(PMU_OUTCOMES)


def test_check_literals_byte_identical_to_migration():
    assert _check("valid_pmue_kind") == "kind IN ('tool_call', 'gap')"
    assert _check("valid_pmue_tool") == (
        "tool IS NULL OR tool IN ('recall', 'reference', 'explore', 'list_tags', "
        "'get_context_info', 'other')"
    )
    assert _check("valid_pmue_outcome") == (
        "outcome IS NULL OR outcome IN ('success', 'degraded', 'denied', 'rate_limited', "
        "'invalid_request', 'error', 'cancelled')"
    )
    assert _check("valid_pmue_era") == "era IS NULL OR era IN ('session', 'stateless')"
    assert _check("valid_pmue_gap_reason") == (
        "gap_reason IS NULL OR gap_reason IN ('queue_full', 'write_failed', 'shutdown')"
    )
    assert _check("pmue_documents_size") == (
        "documents IS NULL OR octet_length(documents::text) <= 4096"
    )


def test_route_and_caller_vocabularies():
    """#3035: append-only; the REST row's tool and era are existing values."""
    assert PMU_CALLERS == ("anonymous", "bound_key")
    assert (PMU_CALLER_ANONYMOUS, PMU_CALLER_BOUND_KEY) == PMU_CALLERS
    assert PMU_ROUTES == ("mcp", "rest")
    assert (PMU_ROUTE_MCP, PMU_ROUTE_REST) == PMU_ROUTES
    assert PMU_SEARCH_TOOL in PMU_TOOLS
    assert PMU_SEARCH_TOOL == "recall"
    assert PMU_REST_ERA in PMU_ERAS
    assert PMU_REST_ERA == "stateless"


def test_f67_check_literals_equal_the_model():
    caller = _check("valid_pmue_caller")
    route = _check("valid_pmue_route")
    shape = _check("pmdv_visitor_key_shape", PublicMcpDailyVisitor)
    assert caller == "caller IS NULL OR caller IN ('anonymous', 'bound_key')"
    assert route == "route IS NULL OR route IN ('mcp', 'rest')"
    assert shape == "visitor_key ~ '^[0-9a-f]{32}$'"
    literals = _f67_text()
    assert f"CHECK ({caller}) NOT VALID" in literals
    assert f"CHECK ({route}) NOT VALID" in literals
    assert shape in _f67_literals()


def test_f67_counts_index_names_the_model_columns():
    index = next(
        i
        for i in PublicMcpUsageEvent.__table_args__
        if isinstance(i, Index) and i.name == "ix_pmue_endpoint_counts"
    )
    keys = [c.name for c in index.columns]
    include = list(index.dialect_options["postgresql"]["include"])
    where = str(index.dialect_options["postgresql"]["where"])
    assert keys == ["endpoint_id", "occurred_at"]
    assert include == ["workspace_id", "kind", "tool", "outcome", "route", "caller"]
    ddl = next(t for t in _f67_literals() if t.startswith("CREATE INDEX CONCURRENTLY"))
    assert ddl == (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pmue_endpoint_counts "
        "ON public_mcp_usage_events (" + ", ".join(keys) + ") "
        "INCLUDE (" + ", ".join(include) + ") "
        "WHERE " + where
    )
