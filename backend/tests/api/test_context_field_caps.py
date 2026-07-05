"""Context metadata length caps — one source of truth across surfaces (#1193).

The prod incident: the MCP ``update_context`` tool documented "max 500 chars"
for ``summary`` but never enforced it, the Slack ingest worker wrote a
516-char summary, and from then on EVERY save from the web settings UI 422'd
(the panel re-submitted all fields) — including a sleep_mode-only change.

These tests pin the fix:
1. REST schemas (ContextCreate/ContextUpdate) use the shared constants —
   summary raised to 2000 so MCP-written production data stays valid.
2. The MCP handlers reject over-cap values instead of silently persisting.
3. The MCP tool definitions advertise the SAME caps via ``maxLength``.
"""

import json

import pytest
from pydantic import ValidationError

from api.routes.contexts import ContextCreate, ContextUpdate
from config.constants import (
    CONTEXT_DESCRIPTION_MAX_LENGTH,
    CONTEXT_SUMMARY_MAX_LENGTH,
    CONTEXT_USAGE_GUIDE_MAX_LENGTH,
)
from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools.context import _validate_context_field_lengths

# ============================================================================
# 1. REST schema caps
# ============================================================================


class TestRestSchemaCaps:
    def test_update_accepts_mcp_written_516_char_summary(self):
        # The exact production shape that used to 422 (#1193).
        update = ContextUpdate(summary="x" * 516)
        assert update.summary is not None

    @pytest.mark.parametrize(
        ("field", "cap"),
        [
            ("description", CONTEXT_DESCRIPTION_MAX_LENGTH),
            ("summary", CONTEXT_SUMMARY_MAX_LENGTH),
            ("usage_guide", CONTEXT_USAGE_GUIDE_MAX_LENGTH),
        ],
    )
    def test_update_at_cap_accepted_over_cap_rejected(self, field: str, cap: int):
        ContextUpdate(**{field: "x" * cap})
        with pytest.raises(ValidationError):
            ContextUpdate(**{field: "x" * (cap + 1)})

    @pytest.mark.parametrize(
        ("field", "cap"),
        [
            ("description", CONTEXT_DESCRIPTION_MAX_LENGTH),
            ("summary", CONTEXT_SUMMARY_MAX_LENGTH),
            ("usage_guide", CONTEXT_USAGE_GUIDE_MAX_LENGTH),
        ],
    )
    def test_create_at_cap_accepted_over_cap_rejected(self, field: str, cap: int):
        ContextCreate(name="demo", **{field: "x" * cap})
        with pytest.raises(ValidationError):
            ContextCreate(name="demo", **{field: "x" * (cap + 1)})


# ============================================================================
# 2. MCP handler-side enforcement
# ============================================================================


class TestMcpHandlerCaps:
    @pytest.mark.parametrize(
        ("field", "cap"),
        [
            ("description", CONTEXT_DESCRIPTION_MAX_LENGTH),
            ("summary", CONTEXT_SUMMARY_MAX_LENGTH),
            ("usage_guide", CONTEXT_USAGE_GUIDE_MAX_LENGTH),
        ],
    )
    def test_over_cap_rejected_with_field_too_long(self, field: str, cap: int):
        result = _validate_context_field_lengths({field: "x" * (cap + 1)})
        assert result is not None
        body = json.loads(result[0].text)
        assert body["error"] == "field_too_long"
        assert field in body["message"]
        assert str(cap) in body["message"]

    @pytest.mark.parametrize(
        ("field", "cap"),
        [
            ("description", CONTEXT_DESCRIPTION_MAX_LENGTH),
            ("summary", CONTEXT_SUMMARY_MAX_LENGTH),
            ("usage_guide", CONTEXT_USAGE_GUIDE_MAX_LENGTH),
        ],
    )
    def test_at_cap_passes(self, field: str, cap: int):
        assert _validate_context_field_lengths({field: "x" * cap}) is None

    def test_absent_and_non_string_fields_pass(self):
        # Field absent → nothing to validate; non-str junk is left for the
        # downstream type handling, not the length gate.
        assert _validate_context_field_lengths({}) is None
        assert _validate_context_field_lengths({"summary": 123}) is None


# ============================================================================
# 3. Tool definitions advertise the same caps
# ============================================================================


class TestToolDefinitionCaps:
    @pytest.mark.parametrize("tool_name", ["create_context", "update_context"])
    def test_definitions_maxlength_matches_constants(self, tool_name: str):
        tool = next(t for t in get_tool_definitions() if t["name"] == tool_name)
        props = tool["inputSchema"]["properties"]
        assert props["description"]["maxLength"] == CONTEXT_DESCRIPTION_MAX_LENGTH
        assert props["summary"]["maxLength"] == CONTEXT_SUMMARY_MAX_LENGTH
        assert props["usage_guide"]["maxLength"] == CONTEXT_USAGE_GUIDE_MAX_LENGTH
