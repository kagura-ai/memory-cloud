"""Instruction / data boundary of the Remote MCP surface (#1682).

The server's own guidance — tool and parameter descriptions, the server
``instructions`` base text and the static quick reference ``get_context_info``
returns — is static, code-reviewed text. What a context's owner or editors
store (``usage_guide``, pinned memories, tool guardrails) and anything ingested
from outside is DATA the server returns when a tool is called. The static text
may say what that data is; it must never tell the model to follow or obey it.

Two guards:

* **Wording** — every tool and parameter description, ``SERVER_INSTRUCTIONS_BASE``
  and ``KAGURA_MEMORY_INSTRUCTIONS`` are scanned for phrasings that told the
  model to follow stored content (the pre-#1682 texts are the fixtures). The
  patterns are narrow on purpose: ``recall``'s "Omit to follow the context's
  search config" and ``explore``'s "Follow only these edge types" describe
  server behaviour and stay allowed.
* **User-directed writes** (#1721, policy 1.F) — memories are client-authored:
  the client composes content and submits it with ``remember`` when the user
  asks. No static text may tell the model to save, store or send conversation
  content (or the conversation itself) on its own or at fixed points (every
  turn, after a task, at the end of a session, when the session ends, as you
  go), to call a tool "after recall()" by habit, or claim pinned memories load
  every turn (only a client hook does that). ``AUTO_SAVE`` holds those
  patterns; the pre-#1721 texts and the auto-save shapes they stand for are
  its fixtures.
* **Delivery** — a Directory connector (OAuth user token, the plain ``/mcp``
  URL, no ``?guardrails=``) gets exactly the static base text from both
  ``initialize`` and ``server/discover``, with public cache hints and no
  database session opened for it.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from mcp_server.tools import get_tool_definitions
from mcp_server.tools._constants import KAGURA_MEMORY_INSTRUCTIONS
from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

# Within one sentence: any character but a newline or a sentence-ending period
# (the dot inside ``context.usage_guide`` does not end one).
_SAME_SENTENCE = r"(?:[^.\n]|\.(?=\w))"
_OBEY = r"\b(follow|obey|comply with|adhere to|execute)\w*\b"

# (pattern, why it is refused). Case-insensitive; matched against one text at
# a time, so a pattern never spans two descriptions.
FORBIDDEN: list[tuple[str, str]] = [
    (
        _OBEY + _SAME_SENTENCE + r"{0,40}usage[_ ]guide",
        "tells the model to follow the owner-written usage_guide",
    ),
    (
        r"usage[_ ]guide"
        + _SAME_SENTENCE
        + r"{0,80}\b(precedence|over generic defaults|overrides?)\b",
        "ranks the owner-written usage_guide above the model's defaults",
    ),
    (
        r"usage[_ ]guide\W{0,4}how (to|an ai should) use",
        "defines usage_guide as instructions for the model",
    ),
    (r"how an ai should use", "defines usage_guide as instructions for the model"),
    (
        r"get_context_info" + _SAME_SENTENCE + r"{0,80}\b(rules|guardrails|polic(y|ies))\b",
        "points the model at get_context_info to fetch rules",
    ),
    (
        _OBEY + _SAME_SENTENCE + r"{0,40}"
        r"\b(guardrails?|pinned|stored (memor|content|note)|memor(y|ies))\b",
        "tells the model to follow stored memories",
    ),
    (r"critical polic(y|ies)", "frames pinned memories as policy to execute"),
    (
        r"influences? your behaviou?r|treated as instructions",
        "implies retrieved content is instructions when it comes from a trusted source",
    ),
]

# #1721 (policy 1.F): writes are user-directed. A save verb and a fixed point
# (every / each / per / after / before / at the end of a turn, task, session,
# conversation ...; "when the session ends", "whenever you finish", "as you go")
# in one sentence, in either order, tells the model to save on its own
# schedule.
#
# Only the base and -ing forms count ("save", "store", "call remember()",
# "saving"): that is how an instruction to the model reads. A description of
# what the server does uses the third-person or past form ("stores", "is
# recorded", "records each step"), which stays allowed. "record", "log" and
# "store" right after a determiner are nouns ("one record", "the audit log").
_NOUN_BEFORE = "".join(
    rf"(?<!\b{word} )"
    for word in ("a", "an", "the", "one", "each", "every", "per", "this", "that", "audit")
)
_SAVE = (
    r"(?:"
    + _NOUN_BEFORE
    + r"\b(?:sav(?:e|ing)|stor(?:e|ing)|remember(?:ing)?|record(?:ing)?|persist(?:ing)?"
    r"|send(?:ing)?|upload(?:ing)?|log(?:ging)?|captur(?:e|ing))\b"
    r"|\b(?:be|get) (?:saved|stored|remembered|recorded|persisted|sent|uploaded|logged"
    r"|captured)\b)"
)
_DET = (
    r"(?:(?:the|a|an|each|every|any|this|that|your|our|its|the current|the whole"
    r"|the entire)\s+)?"
)
_EVENT = r"(?:turn|task|session|conversation|chat|response|message|reply|exchange|step)s?\b"
_FIXED_POINT = (
    "(?:"
    + "|".join(
        [
            # every turn, each task, per session, every 10 turns
            r"\b(?:every|each|per)\s+(?:\d+\s+|other\s+)?" + _EVENT,
            # after each task, before ending the session, at the end of your session
            r"\b(?:after|before|(?:at|by|near|towards?) the (?:end|start|close|conclusion) of"
            + r"|end of)\s+(?:(?:ending|closing|finishing|completing|leaving)\s+)?"
            + _DET
            + _EVENT,
            # at session end
            r"\b(?:at|by)\s+" + _EVENT + r"\s+(?:end|close|exit)\b",
            # when the session ends, once the task is done
            r"\b(?:when|whenever|once|after|before|until)\s+"
            + _DET
            + _EVENT
            + r"\s+(?:ends|is (?:over|done|finished|complete|closing|ending)|finishes|closes"
            + r"|completes)",
            # whenever you finish, before you finish
            r"\b(?:when|whenever|once|after|before)\s+you(?:'re| are)?\s+"
            + r"(?:finish|complete|end|close|leave|wrap up|done)",
            # as you go, along the way
            r"\bas you (?:go|work)\b|\balong the way\b",
            # after recall(), after every recall()
            r"\bafter\s+(?:(?:each|every|any)\s+)?recall\(\)",
        ]
    )
    + ")"
)
AUTO_SAVE: list[tuple[str, str]] = [
    (
        _SAVE + _SAME_SENTENCE + r"{0,160}" + _FIXED_POINT,
        "tells the model to save at fixed points instead of when the user asks",
    ),
    (
        _FIXED_POINT + _SAME_SENTENCE + r"{0,160}" + _SAVE,
        "tells the model to save at fixed points instead of when the user asks",
    ),
    (
        r"\bcall (?:it|this|them)(?: once| again| always)? after\b"
        r"|\bafter (?:each|every|any) (?:recall|search|tool call)",
        "tells the model to call a tool by habit after another one",
    ),
    (
        r"\bsummari[sz](?:e|ing)\b" + _SAME_SENTENCE + r"{0,40}"
        r"\b(?:session|conversation|chat|transcript|thread|discussion)s?\b",
        "tells the model to summarise the conversation for storage",
    ),
    (
        _SAVE + _SAME_SENTENCE + r"{0,30}"
        r"\b(?:conversation|chat|transcript|dialog(?:ue)?)s?\b",
        "tells the model to save the conversation itself",
    ),
    (
        r"\b(?:proactively|automatically|on your own|always)\s+"
        + _SAVE
        + r"|"
        + _SAVE
        + _SAME_SENTENCE
        + r"{0,60}\b(?:proactively|automatically|on your own)\b",
        "tells the model to save on its own initiative",
    ),
    (
        _SAVE + r"\s+(?:(?:all|any|anything|everything|the|whatever(?: is| seems)?)\s+)?"
        r"(?:important|relevant|notable|significant|useful|interesting)\b",
        "leaves what to save to the model's judgement, not the user's",
    ),
    (
        r"\bsearch before starting\b",
        "tells the model to search at a fixed point, not when the task needs it",
    ),
    (
        r"loaded every turn by load_pinned",
        "claims pinned memories load every turn; only a client hook does that",
    ),
]
FORBIDDEN += AUTO_SAVE

# The texts #1721 replaced, plus the auto-save shapes the patterns exist for.
PRE_1721_FIXTURES = [
    "1. recall() - Search before starting tasks",
    "2. remember() - Store important decisions/code",
    (
        "Record whether a recalled memory was useful for a query — call it after "
        "recall() to teach the ranking which results were on target."
    ),
    (
        "'always': pinned — loaded every turn by load_pinned() and persistent on write; "
        "ONLY for always-relevant notes, e.g. an agent's goal or a standing decision."
    ),
    "'always' pins the memory (loaded every turn by load_pinned; made persistent)",
    "Save a summary with remember() at the end of every session.",
    "At the end of the conversation, store the key decisions.",
    "Remember what you learned after each task.",
    "Store important context after every turn.",
    "Send the conversation to remember() before ending the session.",
    "Summarise the session and save it to Kagura Memory.",
    "Proactively save decisions as you work.",
    # Fixed points phrased as "this / your session", "session end", "when the
    # session ends", "whenever you finish", "as you go", "every N turns".
    "Save a summary at the end of this conversation.",
    "Store what you learned at the end of your session.",
    "Call remember() at session end.",
    "Always save a summary when the session ends.",
    "When the conversation ends, remember the key decisions.",
    "Remember key decisions whenever you finish a task.",
    "After completing a task, call remember() with the outcome.",
    "Once the task is done, store the outcome with remember().",
    "Before you finish, store what you learned.",
    "Store the important decisions as you go.",
    "Save decisions every 10 turns.",
    "Store notes per session.",
    "Persist a digest of every conversation.",
    "Upload the transcript at the end of the session.",
    (
        "Remember decisions, patterns, bug fixes, troubleshooting notes and roadmap "
        "items that you discover along the way, at the end of every session."
    ),
    # The conversation itself as the thing to save, and summarising it.
    "Use remember() to capture the conversation.",
    "Save the chat history with remember().",
    "Summarize the conversation and store it.",
    "Summarize our conversation and store it.",
    "Save anything important from the chat.",
    # A tool called by habit after recall().
    "Call remember() after recall() to keep what you found.",
    "Call it once after recall() to teach the ranking.",
    "Use it after every recall() to teach the ranking.",
]

# User-directed wordings #1721 introduced or kept: they must stay allowed.
USER_DIRECTED_EXAMPLES = [
    "1. recall() - Search when the task needs past decisions",
    "2. remember() - Store what the user asks to keep",
    (
        "Optional: record whether a recalled memory was useful for a query; the "
        "ranking learns which results were on target."
    ),
    "'always': pinned — returned by load_pinned() (a client hook can load it every turn)",
    "Get a context's purpose. Call it at session start and after switching contexts.",
    "Use it after recall(), which returns summaries only, when you need the content.",
    "Upsert ephemeral agent run-state at (context_id, key): the current task, step.",
]

# Wordings that describe server behaviour, not stored content: they must stay
# allowed, so a pattern above that starts matching them is too broad.
ALLOWED_EXAMPLES = [
    "Omit to follow the context's search config; false forces it off.",
    "Follow only these edge types: 'neural_association' (automatic), 'related_to'.",
    "later read / download / list / delete access follows that context's ACL.",
    "a recall() that omits use_rerank follows this context's use_rerank.",
    "lint is advisory — the memory is stored; act on a hint with update_memory().",
    # #1721: the save verbs and fixed-point words in a description of what the
    # server does (third-person / past forms, or "record" / "log" as nouns).
    "Returns one record for each message in the thread.",
    "Upload each file separately; the server stores it as-is.",
    "The server logs each request with a correlation id.",
    "Stored memories are returned for each task-scoped context.",
    "Each response includes context_id; the memory is stored before the call returns.",
    "Sleep consolidation records each step of the run.",
    "The feedback is stored with each query as a time series.",
    "set_state stores the current task, step and scratch flags.",
    "Rehydrate an agent's working state at session start in ONE call.",
    "per-phase results, cost tracking and the audit log of every action.",
]

# The texts #1682 replaced. Each must trip at least one pattern, which is what
# keeps the patterns honest.
PRE_1682_FIXTURES = [
    (
        "Call it once at session start and again after switching contexts, and "
        "follow context.usage_guide over generic defaults."
    ),
    (
        "Call list_contexts to discover context IDs, then get_context_info(context_id) "
        "for a context's rules and guardrails, then remember / recall / explore within it."
    ),
    "- context.usage_guide: How to use this context",
    (
        "'always': pinned — loaded every turn by load_pinned() and persistent on write; "
        "ONLY for an agent's goal / guardrail / critical policy."
    ),
    (
        "trust_tier='trusted': excludes external / connector-ingested memories — pass it "
        "for reads that influence your behaviour, so untrusted content is never treated "
        "as instructions."
    ),
    "How an AI should use memories in this context (max 2000 chars).",
    "Follow the context-specific `usage_guide` over generic defaults.",
]


def _descriptions(node: Any, path: str) -> list[tuple[str, str]]:
    """Every ``description`` string under ``node``, with a readable path."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                found.append((path, value))
            else:
                found += _descriptions(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += _descriptions(item, f"{path}[{index}]")
    return found


def _static_texts() -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for tool in get_tool_definitions():
        texts += _descriptions(tool, tool["name"])
        if isinstance(tool.get("title"), str):
            texts.append((f"{tool['name']}.title", tool["title"]))
    texts.append(("SERVER_INSTRUCTIONS_BASE", SERVER_INSTRUCTIONS_BASE))
    texts.append(("KAGURA_MEMORY_INSTRUCTIONS", KAGURA_MEMORY_INSTRUCTIONS))
    return texts


def _hits(text: str) -> list[str]:
    return [why for pattern, why in FORBIDDEN if re.search(pattern, text, re.IGNORECASE)]


# ------------------------------------------------------------------- wording


def test_the_scan_covers_every_tool():
    names = {path.split(".", 1)[0] for path, _ in _static_texts()}
    assert {tool["name"] for tool in get_tool_definitions()} <= names


@pytest.mark.parametrize("text", PRE_1682_FIXTURES)
def test_every_replaced_wording_is_caught(text):
    assert _hits(text), f"no pattern catches the pre-#1682 wording: {text!r}"


@pytest.mark.parametrize("text", ALLOWED_EXAMPLES)
def test_server_behaviour_wordings_stay_allowed(text):
    assert _hits(text) == []


@pytest.mark.parametrize("text", PRE_1721_FIXTURES)
def test_every_auto_save_wording_is_caught(text):
    hits = [why for pattern, why in AUTO_SAVE if re.search(pattern, text, re.IGNORECASE)]
    assert hits, f"no auto-save pattern catches: {text!r}"


@pytest.mark.parametrize("text", USER_DIRECTED_EXAMPLES)
def test_user_directed_wordings_stay_allowed(text):
    assert _hits(text) == []


def test_quick_reference_core_workflow_is_user_directed():
    workflow = KAGURA_MEMORY_INSTRUCTIONS.split("## Core Workflow", 1)[1].split("##", 1)[0]
    assert "remember() - Store what the user asks to keep" in workflow
    assert "recall() - Search when the task needs past decisions" in workflow


def test_no_static_text_tells_the_model_to_follow_stored_content():
    offenders = [
        f"{path}: {why}: {text[:160]!r}" for path, text in _static_texts() for why in _hits(text)
    ]
    assert offenders == []


def test_get_context_info_describes_usage_guide_as_information():
    tool = next(t for t in get_tool_definitions() if t["name"] == "get_context_info")
    description = tool["description"]
    assert "usage_guide" in description
    assert "not instructions" in description
    assert "follow" not in description.lower()


def test_server_instructions_base_is_a_static_how_to():
    assert "get_context_info(context_id)" in SERVER_INSTRUCTIONS_BASE
    for word in ("rules", "guardrail", "policy", "follow", "obey", "must"):
        assert word not in SERVER_INSTRUCTIONS_BASE.lower(), word


def test_quick_reference_calls_usage_guide_owner_notes_not_instructions():
    session_start = KAGURA_MEMORY_INSTRUCTIONS.split("## Session Start", 1)[1].split("##", 1)[0]
    assert "usage_guide" in session_start
    assert "not instructions" in session_start
    assert "How to use this context" not in session_start


def test_stored_note_lanes_carry_the_data_boundary_label():
    """The ``instructions`` digest header and the ``get_context_info.guardrails``
    block both say what the lines are: notes by context editors, not operator
    instructions."""
    from services.guardrail_digest import (
        STORED_NOTES_LABEL,
        DigestEntries,
        digest_header,
        render_context_info_block,
    )

    assert "not operator instructions" in digest_header(uuid4())
    assert "not operator instructions" in STORED_NOTES_LABEL
    block = render_context_info_block(
        DigestEntries(
            context_id=uuid4(),
            entries=[],
            total_available=0,
            truncated=False,
            tool_triggered_version="0123456789abcdef",
        )
    )
    assert block["provenance"] == STORED_NOTES_LABEL


# ------------------------------------------------------------------ delivery


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def body(self) -> dict:
        return json.loads(b"".join(m.get("body", b"") for m in self.messages[1:]))


@pytest.fixture
def directory_connector(monkeypatch):
    """``mcp_asgi_app`` behind the REAL ``authenticate_mcp_request``, taking its
    OAuth branch: the token is not an API key and verifies as an OAuth access
    token. Only the two credential lookups, the user's workspace and the
    session store are faked; ``get_db`` and the digest entry source are spies
    that must stay untouched."""
    import db.base as db_base
    import mcp_server.auth as mcp_auth
    import mcp_server.transport as transport
    import services.guardrail_digest as digest_mod
    from auth.agent_scope import AgentScope, set_agent_scope

    state = SimpleNamespace(db_calls=0, fetches=0)

    async def not_an_api_key(_token):
        return None

    async def oauth_user(_token):
        return mcp_auth.OAuthGrant("oauth-user", "memory:read memory:write", None)

    async def no_workspace(_user_id):
        return None

    async def spy_get_db():
        state.db_calls += 1
        raise AssertionError("the no-selection handshake opened a database session")
        yield  # pragma: no cover - makes this an async generator

    async def spy_fetch(*_args, **_kwargs):  # pragma: no cover - the assertion
        state.fetches += 1
        raise AssertionError("the no-selection handshake read guardrails")

    class _Sessions:
        async def get_or_create_session(self, **_kwargs):
            return SimpleNamespace(session_id="sess-1", user_id="oauth-user", workspace_id=None)

        async def get_session(self, _session_id):  # pragma: no cover - not reached
            return None

    monkeypatch.setattr(mcp_auth, "_verify_api_key", not_an_api_key)
    monkeypatch.setattr(mcp_auth, "_verify_oauth2_token", oauth_user)
    monkeypatch.setattr(transport, "_get_user_workspace_id", no_workspace)
    monkeypatch.setattr(transport, "get_session_manager", lambda: _Sessions())
    monkeypatch.setattr(db_base, "get_db", spy_get_db)
    monkeypatch.setattr(digest_mod, "fetch_entries", spy_fetch)

    # A stale agent scope from an earlier request in a reused context: the
    # OAuth branch must clear it, so no agent-binding digest can apply.
    set_agent_scope(AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=None))

    async def call(body: dict, headers: dict[bytes, bytes] | None = None) -> _Recorder:
        raw = json.dumps(body).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        send = _Recorder()
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp/",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer opaque-oauth-access-token")]
            + list((headers or {}).items()),
        }
        await transport.mcp_asgi_app(scope, receive, send)
        return send

    yield SimpleNamespace(call=call, state=state)
    set_agent_scope(None)


@pytest.mark.asyncio
async def test_directory_connector_initialize_gets_only_the_static_base_text(
    directory_connector,
):
    from auth.agent_scope import get_agent_scope

    send = await directory_connector.call(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
        },
        {b"mcp-protocol-version": b"2025-03-26"},
    )

    assert send.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert get_agent_scope() is None
    assert directory_connector.state.db_calls == 0
    assert directory_connector.state.fetches == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("era", ["legacy", "stateless"])
async def test_directory_connector_discover_gets_the_public_static_base_text(
    directory_connector, era
):
    from mcp_server.transport import DISCOVER_TTL_MS

    body: dict = {"jsonrpc": "2.0", "id": 2, "method": "server/discover"}
    headers: dict[bytes, bytes] = {}
    if era == "stateless":
        body["params"] = {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "ExampleClient", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        }
        headers = {b"mcp-protocol-version": b"2026-07-28", b"mcp-method": b"server/discover"}

    send = await directory_connector.call(body, headers)

    result = send.body["result"]
    assert result["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert result["cacheScope"] == "public"  # private=False: identical for every caller
    assert result["ttlMs"] == DISCOVER_TTL_MS
    assert directory_connector.state.db_calls == 0
    assert directory_connector.state.fetches == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("era", ["legacy", "stateless"])
async def test_build_instructions_without_selection_or_agent_is_base_and_not_private(
    monkeypatch, era
):
    import db.base as db_base
    from auth.agent_scope import set_agent_scope
    from mcp_server.transport import build_instructions

    def must_not_open():  # pragma: no cover - the assertion
        raise AssertionError("opened a database session")

    monkeypatch.setattr(db_base, "get_db", must_not_open)
    set_agent_scope(None)

    instructions, private = await build_instructions(
        user_id="oauth-user", query_string=b"", key_workspace_id=None, era=era
    )

    assert (instructions, private) == (SERVER_INSTRUCTIONS_BASE, False)
