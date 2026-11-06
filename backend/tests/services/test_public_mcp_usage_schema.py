"""Schema v1 of the public MCP usage events: fixtures and forbidden fields (#2678)."""

import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from models.public_mcp_usage import PMU_ERAS, PMU_GAP_REASONS, PMU_OUTCOMES, PMU_TOOLS
from services.public_mcp_usage_events import (
    EraName,
    GapReason,
    OutcomeName,
    PublicMcpUsageCoverageV1,
    PublicMcpUsageDocumentV1,
    PublicMcpUsageEventV1,
    PublicMcpUsageGapV1,
    PublicMcpUsagePageV1,
    ToolName,
    UsageCursor,
    decode_cursor,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "public_mcp_usage_events_v1.json"

# Nothing a request sent, nothing that names a person, a document or a key.
FORBIDDEN_FIELDS = {
    "id",
    "txid",
    "query",
    "query_text",
    "query_hash",
    "text",
    "content",
    "response",
    "result",
    "results",
    "memory_id",
    "memory_ids",
    "public_id",
    "session_id",
    "ip",
    "ip_address",
    "client_ip",
    "user_agent",
    "headers",
    "user_id",
    "name",
    "workspace_name",
    "context_name",
    "client_name",
    "title",
    "summary",
    "source_url",
    "url",
    "doc_id",
    "resource_doc_id",
}


def _fixtures() -> dict:
    return json.loads(FIXTURES.read_text())


@pytest.mark.parametrize("name", sorted(_fixtures()["events"]))
def test_every_fixture_event_validates(name):
    data = _fixtures()["events"][name]
    model = PublicMcpUsageGapV1 if data["kind"] == "gap" else PublicMcpUsageEventV1
    parsed = model.model_validate(data)
    # Round trip: the wire form is the fixture (UTC with Z).
    assert parsed.model_dump(mode="json") == data


def test_fixtures_cover_the_five_tools_and_the_refusals():
    events = _fixtures()["events"].values()
    tools = {e.get("tool") for e in events if e["kind"] == "tool_call"}
    assert {"recall", "reference", "explore", "list_tags", "get_context_info"} <= tools
    outcomes = {e.get("outcome") for e in events}
    assert {"success", "denied", "rate_limited", "cancelled"} <= outcomes
    assert any(e["kind"] == "gap" for e in events)
    assert any(e.get("continuation") for e in events)


def test_page_fixture_validates():
    data = _fixtures()["page"]
    page = PublicMcpUsagePageV1.model_validate(data)
    assert page.client_family_verified is False
    assert page.model_dump(mode="json") == data
    assert decode_cursor(page.next_cursor) == UsageCursor(710444, 42)


def test_models_refuse_unknown_fields():
    data = dict(_fixtures()["events"]["list_tags"])
    data["query"] = "anything"
    with pytest.raises(ValidationError):
        PublicMcpUsageEventV1.model_validate(data)


def test_client_family_verified_cannot_be_true():
    data = dict(_fixtures()["page"])
    data["client_family_verified"] = True
    with pytest.raises(ValidationError):
        PublicMcpUsagePageV1.model_validate(data)


@pytest.mark.parametrize(
    "model",
    [
        PublicMcpUsageEventV1,
        PublicMcpUsageGapV1,
        PublicMcpUsageDocumentV1,
        PublicMcpUsageCoverageV1,
        PublicMcpUsagePageV1,
    ],
)
def test_forbidden_fields_absent(model):
    assert not set(model.model_fields) & FORBIDDEN_FIELDS


def test_wire_literals_match_the_stored_vocabularies():
    assert get_args(ToolName) == PMU_TOOLS
    assert get_args(OutcomeName) == PMU_OUTCOMES
    assert get_args(EraName) == PMU_ERAS
    assert get_args(GapReason) == PMU_GAP_REASONS


def test_documents_are_capped_at_25():
    data = dict(_fixtures()["events"]["recall_two_chunks_of_one_document"])
    data["documents"] = [{"doc_key": f"{i:032x}", "chunks": 1, "rank": i + 1} for i in range(26)]
    with pytest.raises(ValidationError):
        PublicMcpUsageEventV1.model_validate(data)


def test_docs_state_sessions_are_not_people_and_family_is_unverified():
    docs = (Path(__file__).resolve().parents[3] / "docs" / "public-mcp-usage-events.md").read_text()
    assert "Session counts are connections, not people" in docs
    assert "unverified" in docs
    assert '"client_family_verified": false' in docs
