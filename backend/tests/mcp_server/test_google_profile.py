"""The ``google`` tool profile (#2773): a surface for Google's connected apps.

Google's connected-apps policy refuses a tool surface that references other
tools or sets rules for when to use them, that exposes discovery functions, or
that asks the model for the conversation. ``?profile=google`` serves the core
tools minus ``describe_tools`` and ``guide`` with the same schemas, prose that
names no other tool, ``recall`` advertised (and run) read-only, server
instructions without a tool name or guardrail digest, and replies without
static pointers to other tools. Nothing outside that view changes; the core
and full surfaces stay pinned by their own tests.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import structlog

import mcp_server.tools as tools_mod
from mcp_server.tools import execute_tool_call, get_tool_definitions
from mcp_server.tools._annotations import TOOL_ANNOTATIONS
from mcp_server.tools._constants import KAGURA_MEMORY_INSTRUCTIONS
from mcp_server.tools._google_profile import (
    GOOGLE_INSTRUCTIONS_BASE,
    GOOGLE_PROSE,
    _pointer_pattern,
    _without_pointers,
    google_default_context_instructions,
    prose_fingerprint,
)
from mcp_server.tools._helpers import _mcp_tool_view_query
from mcp_server.tools._profiles import (
    CORE_TOOLS,
    GOOGLE_PROFILE,
    GOOGLE_TOOLS,
    PROFILES,
    is_google_view,
    not_listed_reason,
    select_tool_definitions,
)
from tests.mcp_server.test_directory_instruction_boundary import FORBIDDEN

GOOGLE = b"profile=google"
REGISTRY = [tool["name"] for tool in get_tool_definitions()]
_NAME = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, REGISTRY), key=len, reverse=True)) + r")\b"
)

# Wording that asks for (or describes saving) the conversation itself.
PRIVACY_BLOCKLIST = (
    "conversation",
    "chat history",
    "history",
    "transcript",
    "prompt",
    "full context",
    "end-of-session",
    "end of the session",
    "session save",
    "dialogue",
    "everything the user",
)


def _google() -> dict[str, dict]:
    return {tool["name"]: tool for tool in select_tool_definitions(GOOGLE)}


def _descriptions(node, path=()):
    """Every ``description`` string in a tool definition, with its key path."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield path + (key,), value
            else:
                yield from _descriptions(value, path + (key,))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _descriptions(value, path + (i,))


def _without_descriptions(node):
    if isinstance(node, dict):
        return {k: _without_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_without_descriptions(v) for v in node]
    return node


def _other_tools_named(tool_name: str, text: str) -> set[str]:
    return {name for name in _NAME.findall(text) if name != tool_name}


@pytest.fixture
def google_view():
    """Run the test as a request whose URL is ``?profile=google``."""
    token = _mcp_tool_view_query.set(GOOGLE)
    yield
    _mcp_tool_view_query.reset(token)


@pytest.fixture
def core_view():
    token = _mcp_tool_view_query.set(b"profile=core")
    yield
    _mcp_tool_view_query.reset(token)


# ------------------------------------------------------------------ the tool set


def test_the_profile_is_core_without_the_discovery_and_manual_tools():
    assert GOOGLE_PROFILE == "google"
    assert PROFILES["google"] == GOOGLE_TOOLS
    assert GOOGLE_TOOLS == tuple(n for n in CORE_TOOLS if n not in {"describe_tools", "guide"})
    assert list(_google()) == [n for n in REGISTRY if n in GOOGLE_TOOLS]  # registry order


def test_add_tools_cannot_bring_back_describe_tools_or_guide():
    with structlog.testing.capture_logs() as logs:
        names = [
            t["name"]
            for t in select_tool_definitions(
                b"profile=google&add_tools=describe_tools,guide,explore"
            )
        ]
    assert "describe_tools" not in names and "guide" not in names
    assert "explore" in names  # other names work as for core
    (event,) = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert sorted(event["excluded"]) == ["describe_tools", "guide"]


def test_tools_wins_over_the_google_profile():
    """``tools=`` is an explicit allowlist: the URL is no longer the google view."""
    query = b"profile=google&tools=recall,guide"
    assert not is_google_view(query)
    listed = {t["name"]: t for t in select_tool_definitions(query)}
    assert set(listed) == {"recall", "guide"}
    assert listed["recall"]["annotations"] == TOOL_ANNOTATIONS["recall"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (b"profile=google", True),
        (b"profile=%20google%20&context=x", True),
        (b"add_tools=explore&profile=google", True),
        ("profile=google", True),
        (b"profile=core", False),
        (b"profile=Google", False),
        (b"", False),
        (None, False),
        (b"tools=recall&profile=google", False),
        (b"profile=core&profile=google", False),  # the first value wins
    ],
)
def test_the_google_view_is_profile_google_without_tools(query, expected):
    assert is_google_view(query) is expected


# --------------------------------------------------------------------- the prose


def test_no_description_at_any_depth_names_another_tool():
    offenders = []
    for name, tool in _google().items():
        for path, text in _descriptions(tool):
            named = _other_tools_named(name, text)
            if named:
                offenders.append((name, path, sorted(named)))
    assert offenders == []


def test_the_schemas_are_the_core_schemas():
    core = {t["name"]: t for t in select_tool_definitions(b"profile=core")}
    for name, tool in _google().items():
        # The legacy readOnly flag follows readOnlyHint (recall's differs here).
        assert set(tool) - {"readOnly"} == set(core[name]) - {"readOnly"}, name
        assert _without_descriptions(tool["inputSchema"]) == _without_descriptions(
            core[name]["inputSchema"]
        ), name
        assert tool["title"] == core[name]["title"]


def test_every_override_still_matches_the_core_text_it_replaces():
    """A changed core description fails here until its google text is looked at.

    Update the google text in ``_google_profile.GOOGLE_PROSE`` to match the new
    meaning, then its fingerprint (``prose_fingerprint(<new core text>)``).
    """
    core = {t["name"]: t for t in get_tool_definitions()}
    stale = []
    for name, overrides in GOOGLE_PROSE.items():
        assert name in GOOGLE_TOOLS, name
        for path, (fingerprint, text) in overrides.items():
            node = core[name]
            for key in path:
                node = node[key]
            if prose_fingerprint(node) != fingerprint:
                stale.append((name, path, prose_fingerprint(node), node))
            assert text != node, (name, path)  # an override that changes nothing is noise
    assert stale == []


def test_the_core_surface_is_untouched():
    core = {t["name"]: t for t in select_tool_definitions(b"profile=core")}
    full = {t["name"]: t for t in get_tool_definitions()}
    for name in CORE_TOOLS:
        assert core[name] == full[name]
    assert "Manual: guide" in core["recall"]["description"]
    assert core["recall"]["annotations"] == TOOL_ANNOTATIONS["recall"]


def test_the_google_list_does_not_change_the_registry_it_was_built_from():
    before = copy.deepcopy(get_tool_definitions())
    select_tool_definitions(GOOGLE)
    assert get_tool_definitions() == before


def test_the_google_list_is_smaller_than_the_core_list():
    assert len(json.dumps(select_tool_definitions(GOOGLE))) < len(
        json.dumps(select_tool_definitions(b"profile=core"))
    )


def test_no_text_asks_for_the_conversation():
    texts = [text for tool in _google().values() for _p, text in _descriptions(tool)]
    texts += [GOOGLE_INSTRUCTIONS_BASE, google_default_context_instructions("proj")]
    hits = [(word, text) for text in texts for word in PRIVACY_BLOCKLIST if word in text.lower()]
    assert hits == []


# Paraphrased pointers: no tool name, but still a rule for using another tool.
# (A tool describing its own argument, as update_memory's delivery_mode, is fine.)
PARAPHRASE_BLOCKLIST = (
    "store the new one",
    "pins a memory",
    "from search results",
    "unlocked first",
    "of a single stored memory",
)


def test_no_text_paraphrases_a_rule_for_another_tool():
    texts = [text for tool in _google().values() for _path, text in _descriptions(tool)]
    found = {phrase for phrase in PARAPHRASE_BLOCKLIST for text in texts if phrase in text}
    assert found == set()


def test_no_text_tells_the_model_to_follow_stored_content():
    texts = [text for tool in _google().values() for _p, text in _descriptions(tool)]
    texts += [GOOGLE_INSTRUCTIONS_BASE, google_default_context_instructions("proj")]
    hits = [
        (why, text)
        for text in texts
        for pattern, why in FORBIDDEN
        if re.search(pattern, text, re.IGNORECASE)
    ]
    assert hits == []


# ---------------------------------------------------------------- annotations


def test_recall_is_read_only_and_every_other_tool_keeps_its_hints():
    for name, tool in _google().items():
        if name == "recall":
            assert tool["annotations"] == {
                "title": TOOL_ANNOTATIONS["recall"]["title"],
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            }
            assert tool["readOnly"] is True
        else:
            assert tool["annotations"] == TOOL_ANNOTATIONS[name], name
    # The global table and with it the OAuth scope stay as they were.
    assert TOOL_ANNOTATIONS["recall"]["destructiveHint"] is True
    assert TOOL_ANNOTATIONS["recall"]["readOnlyHint"] is False


# --------------------------------------------------------------- instructions


def test_the_instructions_name_no_tool_and_fit_the_base_budget():
    for text in (GOOGLE_INSTRUCTIONS_BASE, google_default_context_instructions("n" * 200)):
        assert len(text) <= 240
        assert _other_tools_named("", text) == set(), text
    assert "Default context: proj-alpha" in google_default_context_instructions("proj-alpha")
    assert google_default_context_instructions("\n\t") == GOOGLE_INSTRUCTIONS_BASE


@pytest.fixture
def digest(monkeypatch):
    """Patch the two instruction reads; record whether the digest was read."""
    import mcp_server.transport as transport

    state = SimpleNamespace(digest_reads=0, name="proj-alpha")

    async def fake_digest(**_kwargs):
        state.digest_reads += 1
        return SimpleNamespace(entries=["x"], context_id=uuid4(), truncated=False)

    async def fake_name(**_kwargs):
        return state.name

    monkeypatch.setattr(transport, "_read_digest", fake_digest)
    monkeypatch.setattr(transport, "_read_default_context_name", fake_name)
    return state


async def _instructions(query: bytes) -> tuple[str, bool]:
    from mcp_server.transport import build_instructions

    return await build_instructions(
        user_id="user-1", query_string=query, key_workspace_id=None, era="stateless"
    )


@pytest.mark.asyncio
async def test_google_instructions_are_the_static_base_without_a_digest(digest):
    assert await _instructions(GOOGLE) == (GOOGLE_INSTRUCTIONS_BASE, False)
    text, _private = await _instructions(b"profile=google&guardrails=" + str(uuid4()).encode())
    assert text == GOOGLE_INSTRUCTIONS_BASE
    assert digest.digest_reads == 0


@pytest.mark.asyncio
async def test_google_instructions_name_the_default_context_without_a_tool(digest):
    text, private = await _instructions(b"profile=google&context=" + str(uuid4()).encode())
    assert (text, private) == (google_default_context_instructions("proj-alpha"), True)
    assert digest.digest_reads == 0


@pytest.mark.asyncio
async def test_core_instructions_still_carry_the_digest(digest):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    with patch("services.guardrail_digest.render_instructions", return_value="digest") as render:
        text, _ = await _instructions(b"profile=core&guardrails=" + str(uuid4()).encode())
    assert text == "digest"
    assert render.call_args.args[0] == SERVER_INSTRUCTIONS_BASE
    assert digest.digest_reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("digest_enabled", [True, False])
async def test_the_rest_preview_of_a_google_url_is_the_google_base(monkeypatch, digest_enabled):
    import services.guardrail_digest as digest_mod
    from api.routes.memory import guardrail_digest
    from config.settings import get_settings
    from services.guardrail_digest import DigestEntries, DigestEntry

    # The MCP handshake decides the google view before lane (a)'s switch.
    monkeypatch.setattr(get_settings(), "mcp_guardrail_digest_enabled", digest_enabled)

    ctx = uuid4()
    entries = DigestEntries(
        context_id=ctx,
        entries=[DigestEntry(str(uuid4()), "a rule", 0.8, True, "manual")],
        total_available=1,
        truncated=False,
        tool_triggered_version="0123456789abcdef",
    )
    monkeypatch.setattr(digest_mod, "fetch_entries", AsyncMock(return_value=entries))

    response = await guardrail_digest(
        user={"user_id": "u1"},
        db=AsyncMock(),
        context_id=str(ctx),
        target="instructions",
        profile="google",
        tools=None,
        add_tools=None,
    )

    assert response.body.decode() == GOOGLE_INSTRUCTIONS_BASE


# --------------------------------------------------------------------- replies


@pytest.fixture
def handlers(monkeypatch):
    """Stub every handler; ``replies[name]`` is the payload it answers with."""
    from mcp_server.tools import _capacity_gate as capacity_gate

    replies: dict[str, tuple[bool, dict]] = {}

    async def no_refusal(*_args, **_kwargs):
        return None

    def stub(name):
        async def handler(_args, _user_id, _workspace_id):
            is_error, payload = replies.get(name, (False, {"status": "success"}))
            content = [tools_mod.TextContent(type="text", text=json.dumps(payload))]
            return tools_mod.ToolErrorContent(content) if is_error else content

        return handler

    monkeypatch.setattr(capacity_gate, "capacity_lock_refusal", no_refusal)
    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", {n: stub(n) for n in REGISTRY})
    with patch.object(tools_mod, "_check_rate_limit", new=AsyncMock(return_value=(True, 0, 100))):
        yield replies


def _payload(result) -> dict:
    return json.loads(result[0].text)


def _names_a_tool(payload: dict, tool_name: str) -> set[str]:
    return _other_tools_named(tool_name, json.dumps(payload))


def _ids() -> dict:
    return {"memory_id": str(uuid4()), "context_id": str(uuid4())}


@pytest.mark.asyncio
async def test_unknown_tool_help_names_no_tool(handlers, google_view):
    payload = _payload(await execute_tool_call("no_such_tool", {}, "u", None))
    assert payload["error"] == "unknown_tool"
    assert payload["help"]
    assert _names_a_tool(payload, "no_such_tool") == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["\x005\x00. list_contexts", "\x000\x00 'x'. list_tags"])
async def test_caller_text_with_a_mask_like_sequence_is_not_a_crash(handlers, google_view, name):
    # The quoted-name mask is NUL-digit-NUL: text that already holds one
    # (here the unknown tool name echoed back) must not raise on restore.
    payload = _payload(await execute_tool_call(name, {}, "u", None))
    assert payload["error"] == "unknown_tool"


def test_pointer_scrub_leaves_text_with_a_nul_alone():
    pattern = _pointer_pattern("recall")
    for text in ("\x005\x00. Call list_contexts.", "Name '\x000\x00'. Call list_tags()."):
        assert _without_pointers(text, pattern) == text


@pytest.mark.asyncio
async def test_a_missing_context_id_carries_no_pointer(handlers, google_view):
    payload = _payload(await execute_tool_call("load_pinned", {}, "u", None))
    assert payload["error"] == "context_id_required"
    assert "?context=" in payload["help"]
    assert _names_a_tool(payload, "load_pinned") == set()


@pytest.mark.asyncio
async def test_a_caller_error_gets_no_manual_pointer(handlers, google_view):
    handlers["recall"] = (True, {"status": "error", "error": "validation_error", "message": "m"})
    result = await execute_tool_call(
        "recall", {"query": "q", "context_id": str(uuid4())}, "u", None
    )
    payload = _payload(result)
    assert payload["error"] == "validation_error"
    assert "help" not in payload


@pytest.mark.asyncio
async def test_core_caller_errors_keep_the_manual_pointer(handlers, core_view):
    handlers["recall"] = (True, {"status": "error", "error": "validation_error", "message": "m"})
    result = await execute_tool_call(
        "recall", {"query": "q", "context_id": str(uuid4())}, "u", None
    )
    assert _payload(result)["help"] == 'Manual: guide(["recall"])'


@pytest.mark.asyncio
async def test_pointer_sentences_leave_help_hint_and_message(handlers, google_view):
    handlers["reference"] = (
        True,
        {
            "status": "error",
            "error": "memory_not_found",
            "message": "Memory not found. Use recall() to find memories you have access to.",
            "help": "Use recall() to get valid memory IDs.",
            "example": 'reference(memory_id="<id from list_contexts>")',
        },
    )
    result = await execute_tool_call("reference", _ids(), "u", None)
    assert isinstance(result, tools_mod.ToolErrorContent)
    assert _payload(result) == {
        "status": "error",
        "error": "memory_not_found",
        "message": "Memory not found.",
    }


@pytest.mark.asyncio
async def test_a_success_hint_loses_its_pointer_sentence(handlers, google_view):
    handlers["list_contexts"] = (
        False,
        {
            "status": "success",
            "contexts": [{"name": "Use recall() often", "id": "x"}],
            "hint": "No contexts are visible to you yet. A workspace owner can create one with "
            'create_context(name="p"). Otherwise ask an owner or admin.',
        },
    )
    payload = _payload(await execute_tool_call("list_contexts", {}, "u", None))
    assert payload["hint"] == "No contexts are visible to you yet. Otherwise ask an owner or admin."
    # Stored data passes through unchanged.
    assert payload["contexts"] == [{"name": "Use recall() often", "id": "x"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_context_info", "bootstrap"])
async def test_the_quick_reference_is_left_out(handlers, google_view, tool):
    handlers[tool] = (
        False,
        {
            "status": "success",
            "context": {"usage_guide": "call recall() first"},
            "instructions": KAGURA_MEMORY_INSTRUCTIONS,
        },
    )
    payload = _payload(await execute_tool_call(tool, {"context_id": str(uuid4())}, "u", None))
    assert "instructions" not in payload
    assert payload["context"] == {"usage_guide": "call recall() first"}  # data, unchanged


@pytest.mark.asyncio
async def test_core_replies_are_unchanged(handlers, core_view):
    payload = {
        "status": "success",
        "hint": "Use recall() to find it.",
        "instructions": KAGURA_MEMORY_INSTRUCTIONS,
    }
    handlers["get_context_info"] = (False, payload)
    result = await execute_tool_call("get_context_info", {"context_id": str(uuid4())}, "u", None)
    assert _payload(result) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "message"),
    [
        ("create_context", "Context 'list_tags' created successfully."),
        ("update_context", "Context 'changes_since-log' updated successfully."),
        ("delete_context", "Context 'recall_upcoming-plans' has been soft-deleted."),
    ],
)
async def test_a_success_message_keeps_a_context_name_that_is_a_tool_name(
    handlers, google_view, tool, message
):
    # The name is the user's data, not a pointer: the reply stays a success.
    handlers[tool] = (False, {"status": "success", "message": message})
    args = {"name": "x"} if tool == "create_context" else {"context_id": str(uuid4())}
    payload = _payload(await execute_tool_call(tool, args, "u", None))
    assert payload == {"status": "success", "message": message}


@pytest.mark.asyncio
async def test_a_success_message_is_never_replaced_by_the_failure_text(handlers, google_view):
    handlers["list_contexts"] = (
        False,
        {"status": "success", "contexts": [], "message": "Call create_context() first."},
    )
    payload = _payload(await execute_tool_call("list_contexts", {}, "u", None))
    assert payload == {"status": "success", "contexts": []}


def _lint_hints() -> list[dict]:
    from services.write_lint import _tag_hints

    no_tags = [h.model_dump(exclude_none=True) for h in _tag_hints([], {})]
    assert no_tags and "list_tags()" in no_tags[0]["hint"]
    return [
        *no_tags,
        {
            "code": "event_date_mismatch",
            # The template of write_lint's event-date hint.
            "hint": "'next friday' is written as (2026-01-01), but the memory holds 2026-01-02 "
            "(event_start..event_end); remove the written date or correct it with "
            "update_memory().",
            "subject": "next friday",
        },
    ]


def _persistence_details() -> list[dict]:
    from services import persistence as persistence_mod

    blocks = [
        persistence_mod._persistence_info("persistent"),
        persistence_mod._persistence_info("persistent", pinned=True),
    ]
    with (
        patch.object(
            persistence_mod,
            "active_consolidation_pass",
            return_value=persistence_mod.SLEEP_CONSOLIDATION,
        ),
        patch.object(persistence_mod, "consolidation_archive_min_age_days", return_value=7),
    ):
        blocks.append(persistence_mod._persistence_info("working"))
    details = [block.model_dump() for block in blocks if block is not None]
    assert all("forget()" in d["detail"] for d in details)
    assert any("reference()" in d["detail"] for d in details)
    return details


def _headroom() -> list[dict]:
    from services.plan_headroom import (
        MCP_CALLS_PER_DAY,
        MEMORIES,
        MEMORIES_PER_DAY,
        QuotaReading,
        headroom_notices,
    )

    def reading(used: int) -> QuotaReading:
        return QuotaReading(
            workspace_id=uuid4(), used=used, limit=100, plan="free", resets_at="2026-11-12T00:00Z"
        )

    notices = headroom_notices(
        {MEMORIES: reading(90), MEMORIES_PER_DAY: reading(96), MCP_CALLS_PER_DAY: reading(85)},
        billing=False,
    )
    assert len(notices) == 3
    return notices


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["remember", "update_memory", "bootstrap"])
async def test_nested_server_text_names_no_tool(handlers, google_view, tool):
    lint, details, headroom = _lint_hints(), _persistence_details(), _headroom()
    handlers[tool] = (
        False,
        {
            "status": "success",
            "memory_id": "m",
            "lint": copy.deepcopy(lint),
            "persistence": copy.deepcopy(details[-1]),
            "plan_headroom": copy.deepcopy(headroom),
        },
    )
    payload = _payload(await execute_tool_call(tool, {"context_id": str(uuid4())}, "u", None))
    assert _names_a_tool(payload, tool) == set()
    # What is left still says what happened; the machine-readable fields stay.
    assert payload["lint"][0]["hint"].startswith("no tags: this memory cannot be reached")
    # A tool may name itself (update_memory's own hint keeps its call).
    assert payload["lint"][1]["hint"].endswith(
        "or correct it with update_memory()."
        if tool == "update_memory"
        else "remove the written date or correct it."
    )
    assert payload["lint"][1]["subject"] == "next friday"
    assert payload["persistence"]["detail"].startswith("Committed and durable now")
    assert "adoption," in payload["persistence"]["detail"]
    assert {k: v for k, v in payload["persistence"].items() if k != "detail"} == {
        k: v for k, v in details[-1].items() if k != "detail"
    }
    for got, sent in zip(payload["plan_headroom"], headroom, strict=True):
        assert {k: v for k, v in got.items() if k != "help"} == {
            k: v for k, v in sent.items() if k != "help"
        }
        assert got["help"].startswith(("Free capacity", "Wait for the daily reset"))
        assert "100 %" in got["help"]


@pytest.mark.asyncio
async def test_a_batch_reply_names_no_tool(handlers, google_view):
    lint, details, headroom = _lint_hints(), _persistence_details(), _headroom()
    item = {"index": 0, "status": "success", "memory_id": "m", "lint": lint}
    handlers["remember_batch"] = (
        False,
        {
            "results": [{**copy.deepcopy(item), "persistence": copy.deepcopy(d)} for d in details],
            "count": len(details),
            "succeeded": len(details),
            "committed_after_timeout": True,
            # batch.py's note when the time limit passed after the commit.
            "message": (
                "The call passed its time limit after the batch was committed: "
                "every item is stored (memory_id per item). Do NOT send the batch "
                "again. Post-commit steps were cut short, so declared links "
                "(supersedes, linked_memory_ids) of some items may be missing — "
                "check with reference()."
            ),
            "plan_headroom": headroom,
            "lint": [{"code": "batch_level_argument", "hint": "Set it once on remember_batch."}],
        },
    )
    payload = _payload(
        await execute_tool_call(
            "remember_batch", {"context_id": str(uuid4()), "items": []}, "u", None
        )
    )
    assert _names_a_tool(payload, "remember_batch") == set()
    assert payload["message"].endswith("of some items may be missing.")
    assert payload["lint"] == [
        {"code": "batch_level_argument", "hint": "Set it once on remember_batch."}
    ]
    assert all(r["persistence"]["detail"] for r in payload["results"])


@pytest.mark.asyncio
async def test_core_nested_server_text_is_unchanged(handlers, core_view):
    payload = {
        "status": "success",
        "lint": _lint_hints(),
        "persistence": _persistence_details()[-1],
        "plan_headroom": _headroom(),
    }
    handlers["remember"] = (False, copy.deepcopy(payload))
    result = await execute_tool_call("remember", {"context_id": str(uuid4())}, "u", None)
    assert _payload(result) == payload


def test_a_hidden_tool_reason_names_no_tool_on_the_google_view():
    reason = not_listed_reason("explore", GOOGLE)
    assert reason is not None
    assert _other_tools_named("", reason) == set()
    assert "add_tools=explore" in (not_listed_reason("explore", b"profile=core") or "")


# ---------------------------------------------------------------- recall writes


def _recall_kwargs(monkeypatch) -> dict:
    """Run the MCP recall handler with the service mocked; return its kwargs."""
    import mcp_server.tools.memory as memory_mod
    from services.memory_service import MemoryService

    seen: dict = {}

    async def fake_recall(self, request, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    async def fake_get_db():
        db = MagicMock()
        db.rollback = AsyncMock()
        yield db

    monkeypatch.setattr(MemoryService, "recall", fake_recall)
    monkeypatch.setattr(
        memory_mod,
        "_resolve_context_for_read",
        AsyncMock(return_value=SimpleNamespace(workspace_id=uuid4())),
    )
    import db.base as db_base

    monkeypatch.setattr(db_base, "get_db", fake_get_db)
    return seen


@pytest.mark.asyncio
async def test_google_recall_asks_the_service_for_no_learning(monkeypatch, google_view):
    from mcp_server.tools.memory import handle_recall

    seen = _recall_kwargs(monkeypatch)
    with contextlib.suppress(RuntimeError):  # the fake service stops the call
        await handle_recall({"query": "q", "context_id": str(uuid4())}, "u", uuid4())
    assert seen["learning"] is False
    assert seen["list_hint"] is False


@pytest.mark.asyncio
async def test_core_recall_still_learns(monkeypatch, core_view):
    from mcp_server.tools.memory import handle_recall

    seen = _recall_kwargs(monkeypatch)
    with contextlib.suppress(RuntimeError):  # the fake service stops the call
        await handle_recall({"query": "q", "context_id": str(uuid4())}, "u", uuid4())
    assert seen["learning"] is True
    assert seen["list_hint"] is True
