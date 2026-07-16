"""Tests for MCP argument JSON-string coercion (#196 / #197).

Some MCP clients serialize arrays, objects, and booleans as JSON strings
over the wire. coerce_mcp_arguments() decodes them back to the types
declared in the tool's JSON schema before handlers run pydantic
validation. These tests pin down the contract for every affected type
plus the no-op cases.
"""

from mcp_server.tools._arg_coercion import coerce_mcp_arguments


class TestArrayCoercion:
    def test_native_list_passes_through(self):
        result = coerce_mcp_arguments("remember", {"tags": ["a", "b"]})
        assert result["tags"] == ["a", "b"]

    def test_json_string_list_is_decoded(self):
        """Regression for #197: tags='[\"a\", \"b\"]' must become ['a', 'b']."""
        result = coerce_mcp_arguments("remember", {"tags": '["a", "b"]'})
        assert result["tags"] == ["a", "b"]

    def test_empty_json_list_decodes(self):
        result = coerce_mcp_arguments("remember", {"tags": "[]"})
        assert result["tags"] == []

    def test_invalid_json_string_passes_through(self):
        """Bad JSON must not raise — pydantic will report a clear error."""
        result = coerce_mcp_arguments("remember", {"tags": "not-json"})
        assert result["tags"] == "not-json"

    def test_non_array_json_string_passes_through(self):
        """JSON-decodable but not an array (e.g. a number): leave for pydantic."""
        result = coerce_mcp_arguments("remember", {"tags": "42"})
        assert result["tags"] == "42"


class TestObjectCoercion:
    def test_native_dict_passes_through(self):
        result = coerce_mcp_arguments("recall", {"query": "x", "filters": {"type": "code"}})
        assert result["filters"] == {"type": "code"}

    def test_json_string_dict_is_decoded(self):
        """Regression for the recall filters bug hit during session-start."""
        result = coerce_mcp_arguments(
            "recall", {"query": "x", "filters": '{"created_after": "2026-03-30T00:00:00Z"}'}
        )
        assert result["filters"] == {"created_after": "2026-03-30T00:00:00Z"}

    def test_invalid_json_string_passes_through(self):
        result = coerce_mcp_arguments("recall", {"query": "x", "filters": "{bad"})
        assert result["filters"] == "{bad"

    def test_non_object_json_string_passes_through(self):
        result = coerce_mcp_arguments("recall", {"query": "x", "filters": "[1, 2]"})
        assert result["filters"] == "[1, 2]"


class TestBooleanCoercion:
    def test_native_true(self):
        result = coerce_mcp_arguments("create_context", {"name": "x", "is_private": True})
        assert result["is_private"] is True

    def test_native_false(self):
        result = coerce_mcp_arguments("create_context", {"name": "x", "is_private": False})
        assert result["is_private"] is False

    def test_string_true_variants(self):
        """Regression for #196: is_private='true' must become True."""
        for raw in ("true", "True", "TRUE", " true ", "1", "yes", "on"):
            result = coerce_mcp_arguments("create_context", {"name": "x", "is_private": raw})
            assert result["is_private"] is True, f"failed for {raw!r}"

    def test_string_false_variants(self):
        for raw in ("false", "False", "FALSE", "0", "no", "off"):
            result = coerce_mcp_arguments("create_context", {"name": "x", "is_private": raw})
            assert result["is_private"] is False, f"failed for {raw!r}"

    def test_ambiguous_string_passes_through(self):
        result = coerce_mcp_arguments("create_context", {"name": "x", "is_private": "maybe"})
        assert result["is_private"] == "maybe"


class TestPassThroughCases:
    def test_empty_arguments(self):
        assert coerce_mcp_arguments("remember", {}) == {}

    def test_unknown_tool_passes_through(self):
        args = {"tags": '["a"]', "is_private": "true"}
        # Unknown tool → no schema → no coercion
        assert coerce_mcp_arguments("not_a_real_tool", args) == args

    def test_unknown_argument_passes_through(self):
        """Arguments not declared in the tool schema are untouched."""
        result = coerce_mcp_arguments("remember", {"summary": "x", "bogus_arg": '["y"]'})
        assert result["bogus_arg"] == '["y"]'

    def test_string_type_not_coerced(self):
        """String-typed fields must not be JSON-decoded."""
        result = coerce_mcp_arguments("remember", {"summary": '["not", "a", "list"]'})
        assert result["summary"] == '["not", "a", "list"]'

    def test_number_type_not_touched(self):
        """Number fields left for pydantic (it already coerces numeric strings)."""
        result = coerce_mcp_arguments("remember", {"importance": "0.8"})
        assert result["importance"] == "0.8"

    def test_does_not_mutate_input_top_level(self):
        """Top-level keys of the input dict must never be replaced in place."""
        original = {"tags": '["a", "b"]'}
        coerced = coerce_mcp_arguments("remember", original)
        assert original["tags"] == '["a", "b"]'  # unchanged
        assert coerced["tags"] == ["a", "b"]
        assert coerced is not original

    def test_already_native_values_are_aliased_not_copied(self):
        """Shallow-copy contract: pass-through values are the same object as the
        caller's original. Callers must not mutate them in place expecting
        isolation. This test pins the contract so a future "fix" to deep-copy
        doesn't silently break performance assumptions."""
        original_tags = ["a", "b"]
        coerced = coerce_mcp_arguments("remember", {"tags": original_tags})
        assert coerced["tags"] is original_tags


class TestAnyCoercion:
    """#1322: typeless ('any') schema fields — set_state's ``value`` — get a
    best-effort JSON decode so quirky clients' stringified values round-trip."""

    def test_stringified_object_is_decoded(self):
        out = coerce_mcp_arguments("set_state", {"value": '{"phase": "running", "n": 1}'})
        assert out["value"] == {"phase": "running", "n": 1}

    def test_stringified_scalars_pass_through(self):
        """Review finding on #1322: retyping "42"→42 / "true"→True would
        corrupt legitimately-string values and diverge from the REST path —
        only unambiguous structures (object/array) are decoded."""
        assert coerce_mcp_arguments("set_state", {"value": "42"})["value"] == "42"
        assert coerce_mcp_arguments("set_state", {"value": "true"})["value"] == "true"

    def test_stringified_array_is_decoded(self):
        assert coerce_mcp_arguments("set_state", {"value": '["a", 1]'})["value"] == ["a", 1]

    def test_nullable_array_field_decodes_stringified_list(self):
        """Fields typed ["array","null"] (bind_agent_context's reserved
        allowed_memory_types) fall into the typeless branch too — a
        stringified list decodes; plain strings pass through to pydantic."""
        out = coerce_mcp_arguments("bind_agent_context", {"allowed_memory_types": '["fact"]'})
        assert out["allowed_memory_types"] == ["fact"]

    def test_plain_string_passes_through(self):
        out = coerce_mcp_arguments("set_state", {"value": "plain string value"})
        assert out["value"] == "plain string value"

    def test_stringified_null_passes_through(self):
        """JSON null would violate NOT NULL storage — keep the literal string."""
        assert coerce_mcp_arguments("set_state", {"value": "null"})["value"] == "null"

    def test_native_object_passes_through(self):
        value = {"a": 1}
        assert coerce_mcp_arguments("set_state", {"value": value})["value"] == {"a": 1}
