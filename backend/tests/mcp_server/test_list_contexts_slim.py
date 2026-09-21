"""#1600: ``list_contexts`` is a slim name→id directory by default.

The tool used to return every context's full ``summary`` (up to 2,000
characters each) plus ``embedding_model``. On a workspace with a few dozen
contexts that is large enough for an MCP client with a tool-result limit to
reject the response outright — and ``list_contexts`` is the first step of every
plugin skill. Agents call it to turn a context NAME into an ID; one context's
details are already available from ``get_context_info``.

Pinned here:

- the default item shape (no ``summary`` / ``embedding_model``) and that the
  ``ContextSearchConfig`` batch query is skipped when it is not needed;
- ``name_contains`` / ``include_summary`` / ``include_details`` semantics;
- the envelope: ``count`` / ``limit`` / ``can_create`` keep their quota meaning
  (``count`` is the workspace-wide context count, NOT the number of items
  returned), so the number of returned items is the new ``total``;
- argument validation → structured ``validation_error``;
- a response-size guard so the waste cannot silently return.

Size assertions on Japanese text are made on the UTF-8 rendering of the
payload: the wire serializer is owned by #1599 (``ensure_ascii``), and the
budget must hold before and after that change. The default shape carries no
free text, so its budget is asserted on the raw wire text as well.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._arg_coercion import coerce_mcp_arguments
from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools.context import handle_list_contexts

SLIM_KEYS = {"id", "name", "is_private", "is_locked", "last_used_at"}
DETAIL_KEYS = SLIM_KEYS | {"summary", "embedding_model"}

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _context(name, *, summary=None, display_name=None, age_days=0):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        display_name=display_name,
        summary=summary,
        is_private=False,
        is_locked=False,
        last_used_at=_NOW - timedelta(days=age_days),
    )


class _Harness:
    """Drive ``handle_list_contexts`` against mocked service + session.

    ``db.execute`` is scripted per statement so a test can tell the
    ``ContextSearchConfig`` batch query from the workspace quota count.
    """

    def __init__(self, contexts, *, configs=(), workspace_count=0, max_contexts=50):
        self.contexts = list(contexts)
        self.statements: list[str] = []

        config_result = MagicMock()
        config_result.scalars.return_value.all.return_value = list(configs)
        count_result = MagicMock()
        count_result.scalar_one.return_value = workspace_count

        async def execute(stmt, *args, **kwargs):
            sql = str(stmt)
            self.statements.append(sql)
            return config_result if "context_search_configs" in sql else count_result

        self.db = AsyncMock()
        self.db.execute = AsyncMock(side_effect=execute)

        self.service = MagicMock()
        self.service.list_contexts = AsyncMock(return_value=self.contexts)
        self.service.get_context_stats = AsyncMock(return_value={"memory_count": 7})

        self.quota = MagicMock()
        self.quota.get_effective_quotas = AsyncMock(return_value={"max_contexts": max_contexts})

    @property
    def config_queries(self):
        return [s for s in self.statements if "context_search_configs" in s]

    async def call(self, args, *, workspace_id=None):
        db = self.db

        async def mock_get_db():
            yield db

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.context_service.ContextService",
                new=MagicMock(return_value=self.service),
            ),
            patch(
                "services.effective_quota_service.EffectiveQuotaService",
                new=MagicMock(return_value=self.quota),
            ),
            patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
        ):
            result = await handle_list_contexts(args, user_id="u1", workspace_id=workspace_id)
        return result[0].text


async def _payload(harness, args, **kwargs):
    return json.loads(await harness.call(args, **kwargs))


# ============================================================================
# Default shape
# ============================================================================


@pytest.mark.asyncio
async def test_default_items_are_slim():
    harness = _Harness([_context("kagura-dev", summary="s" * 2000), _context("notes", age_days=1)])

    payload = await _payload(harness, {})

    assert payload["status"] == "success"
    assert [c["name"] for c in payload["contexts"]] == ["kagura-dev", "notes"]
    for item in payload["contexts"]:
        assert set(item) == SLIM_KEYS
    assert payload["contexts"][0]["last_used_at"].endswith("Z")


@pytest.mark.asyncio
async def test_default_skips_the_search_config_query():
    """``embedding_model`` is the only reason to read ContextSearchConfig."""
    harness = _Harness([_context("a"), _context("b")])

    await harness.call({})
    assert harness.config_queries == []

    await harness.call({"include_summary": True})
    assert harness.config_queries == []

    # Nothing survived the filter → nothing to look the model up for.
    await harness.call({"include_details": True, "name_contains": "no-such-context"})
    assert harness.config_queries == []


@pytest.mark.asyncio
async def test_include_stats_still_adds_memory_count():
    harness = _Harness([_context("a")])

    payload = await _payload(harness, {"include_stats": True})

    assert set(payload["contexts"][0]) == SLIM_KEYS | {"memory_count"}
    assert payload["contexts"][0]["memory_count"] == 7


# ============================================================================
# include_details / include_summary
# ============================================================================


@pytest.mark.asyncio
async def test_include_details_returns_the_previous_item_shape():
    configured = _context("configured", summary="要約" * 1000)
    unconfigured = _context("unconfigured", summary=None, age_days=1)
    config = SimpleNamespace(context_id=configured.id, embedding_model="custom-embedding")
    harness = _Harness([configured, unconfigured], configs=[config])

    with patch("config.settings.get_settings") as get_settings:
        get_settings.return_value.embedding_model = "default-embedding"
        payload = await _payload(harness, {"include_details": True})

    first, second = payload["contexts"]
    assert set(first) == DETAIL_KEYS and set(second) == DETAIL_KEYS
    assert first["summary"] == "要約" * 1000  # full, never truncated
    assert first["embedding_model"] == "custom-embedding"
    assert second["summary"] is None
    assert second["embedding_model"] == "default-embedding"
    assert len(harness.config_queries) == 1  # one batch query, not N+1


@pytest.mark.asyncio
async def test_include_summary_truncates_at_300_characters_and_flags_it():
    long_summary = "あ" * 2000
    harness = _Harness(
        [
            _context("long", summary=long_summary, age_days=0),
            _context("exact", summary="b" * 300, age_days=1),
            _context("short", summary="short summary", age_days=2),
            _context("none", summary=None, age_days=3),
        ]
    )

    payload = await _payload(harness, {"include_summary": True})
    by_name = {c["name"]: c for c in payload["contexts"]}

    assert by_name["long"]["summary"] == "あ" * 300 + "…"
    assert by_name["long"]["summary_truncated"] is True
    assert set(by_name["long"]) == SLIM_KEYS | {"summary", "summary_truncated"}

    # Only items that were actually cut carry the flag.
    assert by_name["exact"]["summary"] == "b" * 300
    assert by_name["short"]["summary"] == "short summary"
    assert by_name["none"]["summary"] is None
    for name in ("exact", "short", "none"):
        assert set(by_name[name]) == SLIM_KEYS | {"summary"}


@pytest.mark.asyncio
async def test_include_details_wins_over_include_summary():
    harness = _Harness([_context("long", summary="x" * 2000)])

    payload = await _payload(harness, {"include_summary": True, "include_details": True})

    item = payload["contexts"][0]
    assert item["summary"] == "x" * 2000
    assert "summary_truncated" not in item
    assert set(item) == DETAIL_KEYS


# ============================================================================
# name_contains
# ============================================================================


@pytest.mark.asyncio
async def test_name_contains_is_a_case_insensitive_trimmed_substring_match():
    harness = _Harness(
        [
            _context("kagura-dev", age_days=2),
            _context("Kagura-Agent-Dev", age_days=0),
            _context("cooking", age_days=1),
        ]
    )

    payload = await _payload(harness, {"name_contains": "  KAGURA "})

    # Recency order is preserved within the filtered list.
    assert [c["name"] for c in payload["contexts"]] == ["Kagura-Agent-Dev", "kagura-dev"]
    assert payload["total"] == 2


@pytest.mark.asyncio
async def test_name_contains_also_matches_the_display_name():
    harness = _Harness(
        [
            _context("kmc", display_name="Memory Cloud Development"),
            _context("other", display_name=None, age_days=1),
        ]
    )

    payload = await _payload(harness, {"name_contains": "cloud dev"})

    assert [c["name"] for c in payload["contexts"]] == ["kmc"]


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   ", None])
async def test_blank_name_contains_means_no_filter(blank):
    harness = _Harness([_context("a"), _context("b", age_days=1)])

    payload = await _payload(harness, {"name_contains": blank})

    assert [c["name"] for c in payload["contexts"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_empty_filter_match_is_a_success_with_an_empty_list():
    harness = _Harness([_context("a"), _context("b")])

    payload = await _payload(harness, {"name_contains": "no-such-context"})

    assert payload["status"] == "success"
    assert payload["contexts"] == []
    assert payload["total"] == 0


@pytest.mark.asyncio
async def test_filter_runs_after_the_permission_scoped_listing():
    """The filter narrows what the permission-scoped service call returned — it
    is never pushed into (or around) that call, so it cannot widen visibility."""
    visible = [_context("team-dev"), _context("team-ops", age_days=1)]
    harness = _Harness(visible)

    payload = await _payload(harness, {"name_contains": "team", "include_stats": True})

    harness.service.list_contexts.assert_awaited_once_with("u1")
    assert {c["id"] for c in payload["contexts"]} <= {str(c.id) for c in visible}

    # include_stats only pays for the contexts that survived the filter.
    await harness.call({"name_contains": "ops", "include_stats": True})
    assert harness.service.get_context_stats.await_count == 2 + 1


# ============================================================================
# Envelope
# ============================================================================


@pytest.mark.asyncio
async def test_envelope_keeps_quota_meaning_and_adds_total():
    """``count`` is the workspace-wide context count used for the quota (it can
    exceed what the caller is allowed to see), so it must not start tracking
    the filter. ``total`` carries the number of returned items."""
    workspace_id = uuid4()
    harness = _Harness(
        [_context("alpha"), _context("beta", age_days=1), _context("gamma", age_days=2)],
        workspace_count=9,
        max_contexts=10,
    )

    unfiltered = await _payload(harness, {}, workspace_id=workspace_id)
    filtered = await _payload(harness, {"name_contains": "alp"}, workspace_id=workspace_id)

    assert (unfiltered["count"], unfiltered["limit"], unfiltered["can_create"]) == (9, 10, True)
    assert (filtered["count"], filtered["limit"], filtered["can_create"]) == (9, 10, True)
    assert unfiltered["total"] == 3
    assert filtered["total"] == 1
    assert len(filtered["contexts"]) == 1


@pytest.mark.asyncio
async def test_count_without_a_workspace_is_the_visible_context_count():
    """No workspace in the session → no quota lookup; ``count`` falls back to
    the number of contexts the caller can see, still independent of the filter."""
    harness = _Harness([_context("alpha"), _context("beta", age_days=1)])

    payload = await _payload(harness, {"name_contains": "alp"})

    assert payload["count"] == 2
    assert payload["total"] == 1
    assert "limit" not in payload and "can_create" not in payload


# ============================================================================
# Validation
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["include_stats", "include_summary", "include_details"])
@pytest.mark.parametrize("value", ["maybe", 1, ["true"]])
async def test_non_boolean_flag_is_a_validation_error(flag, value):
    harness = _Harness([_context("a")])

    payload = await _payload(harness, {flag: value})

    assert payload["status"] == "error"
    assert payload["error"] == "validation_error"
    assert flag in payload["message"]
    harness.service.list_contexts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["x" * 101, 123, ["dev"]])
async def test_invalid_name_contains_is_a_validation_error(value):
    harness = _Harness([_context("a")])

    payload = await _payload(harness, {"name_contains": value})

    assert payload["status"] == "error"
    assert payload["error"] == "validation_error"
    assert "name_contains" in payload["message"]
    harness.service.list_contexts.assert_not_awaited()


@pytest.mark.asyncio
async def test_name_contains_length_is_checked_after_trimming():
    harness = _Harness([_context("x" * 100)])

    payload = await _payload(harness, {"name_contains": "  " + "x" * 100 + "  "})

    assert payload["status"] == "success"
    assert payload["total"] == 1


# ============================================================================
# Size guard
# ============================================================================


def _utf8_length(wire_text: str) -> int:
    """Length of the payload with non-ASCII text as-is (serializer-independent)."""
    return len(json.dumps(json.loads(wire_text), ensure_ascii=False))


@pytest.mark.asyncio
async def test_response_size_budget_for_a_large_workspace():
    """40 contexts × 2,000-character Japanese summaries — the shape that used to
    blow past MCP clients' tool-result limits."""
    contexts = [
        _context(f"project-context-{i:02d}", summary="記憶の要約です。" * 250, age_days=i)
        for i in range(40)
    ]
    assert all(len(c.summary) == 2000 for c in contexts)
    harness = _Harness(contexts, workspace_count=40, max_contexts=100)
    workspace_id = uuid4()

    default_text = await harness.call({}, workspace_id=workspace_id)
    summary_text = await harness.call({"include_summary": True}, workspace_id=workspace_id)
    details_text = await harness.call({"include_details": True}, workspace_id=workspace_id)

    assert len(json.loads(default_text)["contexts"]) == 40
    # No free text in the default shape → the budget holds on the raw wire text
    # under either serialization.
    assert len(default_text) < 8_000
    assert _utf8_length(default_text) < 8_000
    assert _utf8_length(summary_text) < 25_000
    # The opt-in full shape is the old cost — that is what the default avoids.
    assert _utf8_length(details_text) > 80_000


# ============================================================================
# Tool definition
# ============================================================================


def _definition():
    return next(d for d in get_tool_definitions() if d["name"] == "list_contexts")


def test_definition_declares_the_new_parameters():
    props = _definition()["inputSchema"]["properties"]

    assert set(props) == {"include_stats", "name_contains", "include_summary", "include_details"}
    for flag in ("include_stats", "include_summary", "include_details"):
        assert props[flag]["type"] == "boolean"
    assert props["name_contains"]["type"] == "string"
    assert props["name_contains"]["maxLength"] == 100
    assert "required" not in _definition()["inputSchema"]


def test_definition_describes_the_slim_default_and_points_to_get_context_info():
    description = _definition()["description"]

    assert "get_context_info(context_id)" in description
    assert "contexts: [{id, name, is_private, is_locked, last_used_at}]" in description
    assert "total" in description


def test_stringified_flags_are_coerced_before_validation():
    """Quirky clients send booleans as strings (#196/#197); the dispatcher's
    schema-driven coercion must cover the new flags too."""
    coerced = coerce_mcp_arguments(
        "list_contexts", {"include_summary": "true", "include_details": "false"}
    )

    assert coerced == {"include_summary": True, "include_details": False}
