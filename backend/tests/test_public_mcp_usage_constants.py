"""Pins for the public MCP usage event vocabularies (#2678).

The CHECK constraints are rendered from ordered tuples so
``Base.metadata.create_all()`` produces literals byte-identical to the
migration ``f28_2678_public_mcp_usage_events``.
"""

from models.public_mcp_usage import (
    PMU_ERAS,
    PMU_GAP_REASONS,
    PMU_KINDS,
    PMU_OUTCOMES,
    PMU_TOOLS,
    PublicMcpUsageEvent,
)


def _check(name: str) -> str:
    c = next(c for c in PublicMcpUsageEvent.__table_args__ if getattr(c, "name", None) == name)
    return c.sqltext.text


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
