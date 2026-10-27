"""#1403: the MCP tool descriptions must guide an agent through the supersede
auto-suggest loop — recall()/reference() surface a `supersede_candidate`, and the
agent accepts it by creating a `supersedes` edge. These are contract tests: the
guidance is the ONLY thing that turns the (already-forwarded) response field into
an actionable client loop for an agent, so it must not silently disappear.
"""

from mcp_server.tools._definitions import get_tool_definitions


def _all_text(name: str) -> str:
    """All agent-facing text for a tool: top-level description + every parameter
    description (the create_edge supersede guidance lives on the edge_type param)."""
    tool = next(t for t in get_tool_definitions() if t["name"] == name)
    parts = [tool.get("description", "")]
    props = tool.get("inputSchema", {}).get("properties", {})
    for prop in props.values():
        if isinstance(prop, dict) and isinstance(prop.get("description"), str):
            parts.append(prop["description"])
    return "\n".join(parts)


# #2308: the accept path every client can call — a ?tools= allowlist may list
# update_memory without create_edge (core lists both since #2353).
CORE_ACCEPT = "update_memory(accept_supersede_candidate=true)"


def test_recall_documents_supersede_candidate_and_accept_flow():
    d = _all_text("recall")
    assert "supersede_candidate" in d
    # The accept action must be spelled out, on a tool the core profile lists.
    assert CORE_ACCEPT in d
    # And the "suggestion only" contract (never auto-applied) must be stated.
    assert "SUGGESTION" in d or "suggestion" in d


def test_reference_documents_supersede_candidate_and_accept_flow():
    d = _all_text("reference")
    assert "supersede_candidate" in d
    assert CORE_ACCEPT in d
    # create_edge stays documented as the general way to link a pair.
    assert "create_edge" in d
    assert 'edge_type="supersedes"' in d


def test_update_memory_offers_the_accept_flag():
    tool = next(t for t in get_tool_definitions() if t["name"] == "update_memory")
    prop = tool["inputSchema"]["properties"]["accept_supersede_candidate"]
    assert prop["type"] == "boolean"
    assert "memory_id" in prop["description"]
    assert "superseded_memory_id?" in tool["description"]


def test_guide_topics_name_the_core_accept_path():
    from mcp_server.tools.guide import GUIDE_TOPICS

    for topic in ("remember.supersedes", "update_memory.modes", "reference.returns"):
        assert "accept_supersede_candidate" in GUIDE_TOPICS[topic], topic
    assert "superseded_memory_id" in GUIDE_TOPICS["update_memory.returns"]
    recall_topics = " ".join(text for key, text in GUIDE_TOPICS.items() if key.startswith("recall"))
    assert "accept_supersede_candidate=true" in recall_topics


def test_create_edge_documents_accepting_a_supersede_candidate():
    d = _all_text("create_edge")
    # create_edge is the acceptance entry point for a supersede_candidate.
    assert "supersede_candidate" in d
    assert "supersedes" in d


def test_remember_points_to_the_auto_suggest_fallback():
    d = _all_text("remember")
    # remember() should tell the agent that a missed supersedes is auto-detected
    # and surfaced later as a supersede_candidate.
    assert "supersede_candidate" in d


def test_supersede_guidance_carries_no_bare_issue_ids():
    """Agent-facing description text must not carry bare issue-tracker IDs an
    agent cannot resolve — provenance belongs in the commit/PR, not the tool
    contract. Guards the #1403 additions (lines that mention supersede_candidate).
    Pre-existing unrelated ids (e.g. #1208 on the supersedes edge-type line) are
    out of scope and intentionally not asserted against."""
    for name in ("recall", "reference", "create_edge", "remember"):
        for line in _all_text(name).splitlines():
            if "supersede_candidate" in line:
                assert "#1403" not in line and "#1299" not in line, (
                    f"{name}: bare issue id in agent-facing supersede guidance: {line!r}"
                )
