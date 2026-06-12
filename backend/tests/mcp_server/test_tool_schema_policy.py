"""Pre-1.0 MCP tool inputSchema policy guards (#990).

These pin the schema-rigor decisions made for the 1.0 freeze so they cannot
silently regress: every tool's argument object is strict (no undeclared
top-level params), and the reserved-for-v1.5 no-op param was removed.
"""

from mcp_server.tools._definitions import get_tool_definitions


def test_all_object_schemas_are_strict():
    """Every tool inputSchema forbids undeclared top-level parameters (#990).

    additionalProperties is applied centrally in get_tool_definitions, so a new
    tool that forgets it (or sets it true) is a policy regression caught here.
    """
    defs = get_tool_definitions()
    offenders = [
        d["name"]
        for d in defs
        if d.get("inputSchema", {}).get("type") == "object"
        and d["inputSchema"].get("additionalProperties") is not False
    ]
    assert offenders == [], f"tools missing additionalProperties:false: {offenders}"


def test_analyze_context_query_noop_removed():
    """The reserved-for-v1.5 no-op ``query`` param is gone from the schema (#990).

    Shipping a documented no-op into a frozen 1.0 surface invites silent failure
    (clients pass it expecting an effect). It will be re-added in v1.5 when the
    query-scoped analysis path is actually implemented.
    """
    defs = get_tool_definitions()
    analyze = next(d for d in defs if d["name"] == "analyze_context")
    assert "query" not in analyze["inputSchema"]["properties"]
