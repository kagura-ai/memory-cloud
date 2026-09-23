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
from services.context_service import ContextListingFailedError, ContextService

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
    harness = _Harness([_context("acme-web", summary="s" * 2000), _context("notes", age_days=1)])

    payload = await _payload(harness, {})

    assert payload["status"] == "success"
    assert [c["name"] for c in payload["contexts"]] == ["acme-web", "notes"]
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
@pytest.mark.parametrize(
    ("extra", "expected_keys"),
    [
        ({}, SLIM_KEYS | {"memory_count"}),
        (
            {"include_summary": True},
            SLIM_KEYS | {"summary", "summary_truncated", "memory_count"},
        ),
        ({"include_details": True}, DETAIL_KEYS | {"memory_count"}),
        (
            {"include_summary": True, "include_details": True},
            DETAIL_KEYS | {"memory_count"},
        ),
    ],
)
async def test_include_stats_adds_memory_count_to_every_item_shape(extra, expected_keys):
    """``include_stats`` is orthogonal to the shape flags: ``memory_count`` rides
    on the slim, preview and detailed items alike."""
    harness = _Harness([_context("a", summary="s" * 301)])

    payload = await _payload(harness, {"include_stats": True, **extra})

    item = payload["contexts"][0]
    assert set(item) == expected_keys
    assert item["memory_count"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["include_stats", "include_summary", "include_details"])
async def test_explicit_null_flag_means_omitted(flag):
    """Clients that serialise unset optionals as JSON ``null`` keep working: a
    null flag is the default (slim) shape, not a ``validation_error`` — the same
    rule ``name_contains=null`` follows, and what ``include_stats=null`` did
    before #1600."""
    harness = _Harness([_context("a", summary="s" * 2000)])

    # Through the dispatcher's coercion, which must leave ``null`` alone.
    payload = await _payload(harness, coerce_mcp_arguments("list_contexts", {flag: None}))

    assert payload["status"] == "success"
    assert set(payload["contexts"][0]) == SLIM_KEYS
    assert harness.config_queries == []
    harness.service.get_context_stats.assert_not_awaited()


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
            _context("over", summary="c" * 301, age_days=4),
        ]
    )

    payload = await _payload(harness, {"include_summary": True})
    by_name = {c["name"]: c for c in payload["contexts"]}

    assert by_name["long"]["summary"] == "あ" * 300 + "…"
    assert by_name["long"]["summary_truncated"] is True
    assert set(by_name["long"]) == SLIM_KEYS | {"summary", "summary_truncated"}

    # The boundary is 300, not 301: one character over is cut and flagged.
    assert by_name["over"]["summary"] == "c" * 300 + "…"
    assert by_name["over"]["summary_truncated"] is True
    assert set(by_name["over"]) == SLIM_KEYS | {"summary", "summary_truncated"}

    # Only items that were actually cut carry the flag.
    assert by_name["exact"]["summary"] == "b" * 300
    assert by_name["short"]["summary"] == "short summary"
    assert by_name["none"]["summary"] is None
    for name in ("exact", "short", "none"):
        assert set(by_name[name]) == SLIM_KEYS | {"summary"}


@pytest.mark.asyncio
async def test_include_summary_cuts_on_code_points_not_bytes():
    """The 300th character is an astral emoji (4 UTF-8 bytes, a UTF-16 surrogate
    pair): a byte- or UTF-16-based cut would drop it or leave half of it."""
    harness = _Harness([_context("astral", summary="a" * 299 + "\U0001f600" + "b" * 50)])

    text = await harness.call({"include_summary": True})
    item = json.loads(text)["contexts"][0]

    assert item["summary"] == "a" * 299 + "\U0001f600" + "…"
    assert item["summary_truncated"] is True
    item["summary"].encode("utf-8")  # no lone surrogate survives the round trip


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
            _context("acme-web", age_days=2),
            _context("Acme-Mobile", age_days=0),
            _context("cooking", age_days=1),
        ]
    )

    payload = await _payload(harness, {"name_contains": "  ACME "})

    # Recency order is preserved within the filtered list.
    assert [c["name"] for c in payload["contexts"]] == ["Acme-Mobile", "acme-web"]
    assert payload["total"] == 2


@pytest.mark.asyncio
async def test_name_contains_also_matches_the_display_name():
    harness = _Harness(
        [
            _context("handbook", display_name="Alpha Team Handbook"),
            _context("other", display_name=None, age_days=1),
        ]
    )

    payload = await _payload(harness, {"name_contains": "team hand"})

    assert [c["name"] for c in payload["contexts"]] == ["handbook"]


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

    harness.service.list_contexts.assert_awaited_once_with("u1", raise_on_lookup_error=True)
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


# ============================================================================
# #1658: empty-account hint
# ============================================================================


@pytest.mark.asyncio
async def test_no_visible_context_adds_a_hint_naming_create_context():
    """A new account (or a member with no access) sees an empty list; the hint
    says to create a context and what to do when create_context is not in the
    client's tool list (``?profile=core`` leaves it out)."""
    harness = _Harness([], workspace_count=0)

    payload = await _payload(harness, {}, workspace_id="ws-1")

    assert payload["status"] == "success"
    assert payload["contexts"] == []
    hint = payload["hint"]
    assert hint.startswith("No contexts are visible to you yet.")
    assert "create_context(" in hint
    # Owner/admin only (create_context refuses members), so a member is told
    # to ask for a context or for access instead.
    assert "owner or admin" in hint
    assert "give you access" in hint
    # The way forward when tools/list has no create_context.
    assert "create_context is not in your tool list" in hint
    assert "web UI" in hint
    assert "without ?profile=core" in hint


@pytest.mark.asyncio
async def test_hint_tells_an_admin_to_pass_is_private_false():
    """create_context defaults to is_private=true and only an owner may create a
    private context, so the bare call the hint shows fails for an admin; the
    hint names the argument an admin needs."""
    payload = await _payload(_Harness([]), {}, workspace_id="ws-1")

    assert "owner can create one with create_context(" in payload["hint"]
    assert "an admin must add is_private=false" in payload["hint"]


@pytest.mark.asyncio
async def test_create_context_default_is_private_is_owner_only():
    """Pins the rule the admin half of the hint relies on: an admin calling
    create_context with the default is_private is refused by the service."""
    from auth.workspace_roles import WorkspaceRole
    from utils.exceptions import ValidationError

    workspace = SimpleNamespace(id=uuid4(), plan_name="pro")
    member = SimpleNamespace(role=WorkspaceRole.ADMIN)
    results = iter([workspace, member])

    db = AsyncMock()

    async def execute(*_a, **_k):
        result = MagicMock()
        result.scalar_one_or_none.return_value = next(results, member)
        return result

    db.execute = AsyncMock(side_effect=execute)

    with pytest.raises(ValidationError, match="Only workspace owners can create private"):
        await ContextService(db).create_context(
            workspace_id=workspace.id, name="my-project", created_by="u1"
        )


@pytest.mark.asyncio
async def test_no_workspace_hint_does_not_suggest_create_context():
    """Without a workspace create_context refuses with workspace_required, so the
    hint must not tell the caller to call it."""
    payload = await _payload(_Harness([]), {}, workspace_id=None)

    hint = payload["hint"]
    assert "no current workspace" in hint
    assert "create_context(" not in hint
    assert "create_context cannot run yet" in hint
    assert "web UI" in hint


@pytest.mark.asyncio
async def test_failed_access_lookup_is_not_an_empty_account():
    """A permission or database failure inside the access lookup keeps the empty
    success it has always answered, but carries no hint — the caller is not
    told to create a context when the listing simply failed."""
    harness = _Harness([], workspace_count=3)
    harness.service.list_contexts = AsyncMock(
        side_effect=ContextListingFailedError("not a member of workspace")
    )

    payload = await _payload(harness, {}, workspace_id="ws-1")

    assert payload["status"] == "success"
    assert payload["contexts"] == []
    assert "hint" not in payload


@pytest.mark.asyncio
async def test_other_listing_failures_keep_their_error_envelope():
    """Only the lookup failure the service used to swallow is caught; anything
    else (a timeout, a failed workspace lookup) is still an error response."""
    harness = _Harness([])
    harness.service.list_contexts = AsyncMock(side_effect=RuntimeError("db down"))

    payload = await _payload(harness, {}, workspace_id="ws-1")

    assert payload["status"] == "error"
    assert "hint" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_service_lookup_failure_is_empty_by_default_and_raises_on_request(strict):
    """ContextService.list_contexts keeps its empty-list fallback for every other
    caller (the REST route); MCP asks for the failure to be raised."""
    service = ContextService(AsyncMock())
    perm = MagicMock()
    perm.get_accessible_contexts = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch.object(
            ContextService, "_get_user_current_workspace_id", AsyncMock(return_value=uuid4())
        ),
        patch("services.permission_service.PermissionService", return_value=perm),
    ):
        if strict:
            with pytest.raises(ContextListingFailedError):
                await service.list_contexts("u1", raise_on_lookup_error=True)
        else:
            assert await service.list_contexts("u1") == []


@pytest.mark.asyncio
async def test_member_with_no_access_gets_the_hint_even_when_the_workspace_has_contexts():
    """``count`` is workspace-wide quota usage; the hint follows what the caller
    can see, so a member without access to any of 3 contexts still gets it."""
    harness = _Harness([], workspace_count=3)

    payload = await _payload(harness, {}, workspace_id="ws-1")

    assert payload["count"] == 3
    assert "hint" in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"name_contains": "no-such-context"}])
async def test_no_hint_when_the_caller_can_see_a_context(args):
    """Visible contexts, or a name_contains that matches none of them, keep
    the pre-#1658 envelope — an empty filter match is not an empty account."""
    harness = _Harness([_context("a")], workspace_count=1)

    payload = await _payload(harness, args, workspace_id="ws-1")

    assert "hint" not in payload
    assert set(payload) == {"status", "contexts", "count", "total", "limit", "can_create"}


def test_list_contexts_description_mentions_the_hint():
    (tool,) = [t for t in get_tool_definitions() if t["name"] == "list_contexts"]
    assert "hint" in tool["description"]
