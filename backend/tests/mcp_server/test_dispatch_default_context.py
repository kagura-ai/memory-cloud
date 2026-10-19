"""The dispatcher fills a missing ``context_id`` from ``?context=`` (#2125).

``execute_tool_call`` writes the connection default into the arguments after
coercion and before the capacity gate, the pre-dispatch check and the
handler. Pinned here:

* precedence — explicit ``context_id`` > recall's ``context_ids`` > the URL;
* the exempt set — ``delete_context()`` under a default never reaches its
  handler, and no exempt tool ever receives the default;
* a ``?context=`` that is not a UUID is named, not read as "no default";
* the capacity gate sees the injected id;
* the schemas — ``context_id`` left ``required`` exactly where it is not
  defaulted;
* parity — a default naming a context the caller cannot use answers with the
  same ``error`` and ``message`` as the explicit id (the real handler and the
  real resolvers run; only the services below them are stubbed).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

import mcp_server.tools as tools_mod
import mcp_server.tools._capacity_gate as capacity_gate
from auth.agent_scope import AgentScope, set_agent_scope
from mcp_server.tools import (
    _DEFAULT_CONTEXT_EXEMPT,
    _TOOLS_WITHOUT_CONTEXT_ID,
    execute_tool_call,
    get_tool_definitions,
    takes_default_context,
)
from mcp_server.tools._default_context import DefaultContextSelection
from mcp_server.tools._helpers import (
    set_mcp_default_context_selection,
    set_mcp_key_workspace_scope,
)

DEFAULT = uuid4()
URL = DefaultContextSelection("url", DEFAULT)

# The 30 tools whose context_id left ``required``, plus recall (never had it).
DEFAULTED = {
    "remember",
    "remember_batch",
    "update_memory",
    "reference",
    "list_revisions",
    "recall_upcoming",
    "recall_nearby",
    "load_pinned",
    "load_guardrails",
    "forget",
    "explore",
    "list_edges",
    "create_edge",
    "update_edge",
    "delete_edge",
    "get_context_info",
    "list_tags",
    "get_sleep_history",
    "rollback_session",
    "analyze_context",
    "list_analyses",
    "get_active_analysis",
    "feedback",
    "list",
    "changes_since",
    "bootstrap",
    "set_state",
    "get_state",
    "record_measurement",
    "recall_series",
    "recall",
}


@pytest.fixture(autouse=True)
def _clean_contextvars():
    set_mcp_default_context_selection(None)
    set_mcp_key_workspace_scope(None)
    set_agent_scope(None)
    yield
    set_mcp_default_context_selection(None)
    set_mcp_key_workspace_scope(None)
    set_agent_scope(None)


@pytest.fixture
def gate(monkeypatch):
    """The capacity gate, recording the arguments it was asked about."""
    seen: list[dict] = []

    async def spy(tool_name, args, user_id, workspace_id):
        seen.append(dict(args))
        return None

    monkeypatch.setattr(capacity_gate, "capacity_lock_refusal", spy)
    return seen


@pytest.fixture
def handlers(monkeypatch, gate):
    """Registry of stubs that record the arguments each tool receives."""
    received: dict[str, dict] = {}

    def stub(name):
        async def handler(args, _user_id, _workspace_id):
            received[name] = args
            return [tools_mod.TextContent(type="text", text='{"status":"success"}')]

        return handler

    registry = {d["name"]: stub(d["name"]) for d in get_tool_definitions()}
    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", registry)
    with patch.object(tools_mod, "_check_rate_limit", new=AsyncMock(return_value=(True, 0, 100))):
        yield received


def _payload(result) -> dict:
    return json.loads(result[0].text)


def _remember_args(**extra) -> dict:
    return {"summary": "a summary long enough", "content": "c", "type": "note", **extra}


# ------------------------------------------------------------------ precedence


@pytest.mark.asyncio
async def test_a_call_without_context_id_runs_in_the_url_default(handlers):
    set_mcp_default_context_selection(URL)

    result = await execute_tool_call("remember", _remember_args(), "user-1", None)

    # A response that does not name its context gets the default's id.
    assert _payload(result) == {"status": "success", "context_id": str(DEFAULT)}
    assert handlers["remember"]["context_id"] == str(DEFAULT)


@pytest.mark.asyncio
async def test_an_explicit_context_id_wins_over_the_url(handlers):
    set_mcp_default_context_selection(URL)
    explicit = str(uuid4())

    await execute_tool_call("remember", _remember_args(context_id=explicit), "user-1", None)

    assert handlers["remember"]["context_id"] == explicit


@pytest.mark.asyncio
async def test_a_null_context_id_is_absent(handlers):
    """A client that sends every optional key as null still gets the default."""
    set_mcp_default_context_selection(URL)

    await execute_tool_call("get_context_info", {"context_id": None}, "user-1", None)

    assert handlers["get_context_info"]["context_id"] == str(DEFAULT)


@pytest.mark.asyncio
async def test_recall_with_context_ids_is_not_given_the_default(handlers):
    set_mcp_default_context_selection(URL)
    ids = [str(uuid4()), str(uuid4())]

    await execute_tool_call("recall", {"query": "q", "context_ids": ids}, "user-1", None)

    assert "context_id" not in handlers["recall"]
    assert handlers["recall"]["context_ids"] == ids


@pytest.mark.asyncio
async def test_recall_without_either_gets_the_default(handlers):
    set_mcp_default_context_selection(URL)

    await execute_tool_call("recall", {"query": "q"}, "user-1", None)

    assert handlers["recall"]["context_id"] == str(DEFAULT)


@pytest.mark.asyncio
async def test_the_caller_dict_is_not_mutated(handlers):
    set_mcp_default_context_selection(URL)
    args = _remember_args()

    await execute_tool_call("remember", args, "user-1", None)

    assert "context_id" not in args


@pytest.mark.asyncio
async def test_the_capacity_gate_sees_the_injected_id(handlers, gate):
    """The gate reads ``args["context_id"]`` itself; without it the gate would
    fall back to the session workspace — the wrong gate for a default."""
    set_mcp_default_context_selection(URL)

    await execute_tool_call("remember", _remember_args(), "user-1", uuid4())

    assert gate[-1]["context_id"] == str(DEFAULT)
    assert capacity_gate._argument_targets(gate[-1])[0] == {DEFAULT}


# ------------------------------------------------------------------ exempt set


def test_the_exempt_set_is_pinned():
    assert _DEFAULT_CONTEXT_EXEMPT == {
        "delete_context",
        "update_search_config",
        "update_context",
        "merge_contexts",
        "bind_agent_context",
        "describe_binding",
        "get_agent_bootstrap",
        "list_contexts",
        "create_context",
        "setup_connector",
        "init_file_upload",
    }


def test_exactly_the_defaulted_tools_take_the_default():
    names = {d["name"] for d in get_tool_definitions()}
    assert {n for n in names if takes_default_context(n)} == DEFAULTED


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(_DEFAULT_CONTEXT_EXEMPT))
async def test_no_exempt_tool_ever_receives_the_default(handlers, tool):
    set_mcp_default_context_selection(URL)

    result = await execute_tool_call(tool, {}, "user-1", None)

    assert handlers.get(tool, {}).get("context_id") is None
    if tool not in _TOOLS_WITHOUT_CONTEXT_ID:
        # In the pre-dispatch gate: refused before the handler, old help.
        payload = _payload(result)
        assert payload["error"] == "context_id_required"
        assert "?context=" not in payload["help"]
        assert tool not in handlers


@pytest.mark.asyncio
async def test_a_bare_delete_context_never_reaches_its_handler(handlers):
    set_mcp_default_context_selection(URL)

    result = await execute_tool_call("delete_context", {}, "user-1", uuid4())

    assert _payload(result)["error"] == "context_id_required"
    assert "delete_context" not in handlers


# ---------------------------------------------------------------- refusals


@pytest.mark.asyncio
async def test_an_invalid_url_value_is_named_not_read_as_no_default(handlers, gate):
    set_mcp_default_context_selection(DefaultContextSelection("invalid", raw_length=10))

    result = await execute_tool_call("remember", _remember_args(), "user-1", uuid4())

    assert getattr(result, "is_error", False) is True
    payload = _payload(result)
    assert payload["error"] == "invalid_context_id_format"
    assert payload["context_source"] == "url"
    assert "?context=" in payload["message"]
    assert "remember" not in handlers
    assert gate == []  # refused before anything else looked at the call


@pytest.mark.asyncio
async def test_an_invalid_url_value_does_not_affect_an_explicit_call(handlers):
    set_mcp_default_context_selection(DefaultContextSelection("invalid", raw_length=3))
    explicit = str(uuid4())

    result = await execute_tool_call("remember", _remember_args(context_id=explicit), "u", None)

    assert _payload(result) == {"status": "success"}  # explicit: nothing added
    assert handlers["remember"]["context_id"] == explicit


@pytest.mark.asyncio
async def test_an_invalid_url_value_does_not_affect_tools_without_a_context(handlers):
    set_mcp_default_context_selection(DefaultContextSelection("invalid", raw_length=3))

    result = await execute_tool_call("list_contexts", {}, "user-1", None)

    assert _payload(result) == {"status": "success"}


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [None, DefaultContextSelection("none")])
async def test_without_a_default_the_call_is_context_id_required(handlers, selection):
    set_mcp_default_context_selection(selection)

    result = await execute_tool_call("remember", _remember_args(), "user-1", None)

    payload = _payload(result)
    assert payload["error"] == "context_id_required"
    assert payload["message"] == "remember requires context_id argument."
    assert payload["help"].startswith(
        "Pass context_id, or set a default for this connection with ?context=<uuid>"
    )
    assert 'guide(["remember"])' in payload["help"]
    assert "context_source" not in payload


@pytest.mark.asyncio
async def test_a_context_not_found_under_the_default_names_its_source(monkeypatch, gate):
    envelope = tools_mod._error_response(
        "context_not_found",
        "Context not found or you don't have access to it.",
        context_id=str(DEFAULT),
        help="Use list_contexts() to see contexts you have access to.",
    )

    async def deny(_args, _user_id, _workspace_id):
        return envelope

    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", {"get_context_info": deny})
    set_mcp_default_context_selection(URL)
    implicit = _payload(await execute_tool_call("get_context_info", {}, "user-1", None))
    explicit = _payload(
        await execute_tool_call("get_context_info", {"context_id": str(DEFAULT)}, "user-1", None)
    )

    assert explicit == json.loads(envelope[0].text)
    assert implicit["context_source"] == "url"
    assert "?context=" in implicit["help"]
    for key in ("status", "error", "message", "context_id"):
        assert implicit[key] == explicit[key]


@pytest.mark.asyncio
async def test_a_success_under_the_default_names_its_context(handlers):
    set_mcp_default_context_selection(URL)

    result = await execute_tool_call("set_state", {"key": "k", "value": 1}, "user-1", None)

    assert _payload(result) == {"status": "success", "context_id": str(DEFAULT)}
    assert not getattr(result, "is_error", False)


@pytest.mark.asyncio
async def test_a_success_that_names_its_context_is_left_alone(monkeypatch, gate):
    own = json.dumps({"status": "success", "context_id": str(DEFAULT), "context_name": "a"})

    async def ok(_args, _user_id, _workspace_id):
        return [tools_mod.TextContent(type="text", text=own)]

    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", {"get_context_info": ok})
    set_mcp_default_context_selection(URL)

    result = await execute_tool_call("get_context_info", {}, "user-1", None)

    assert result[0].text == own


@pytest.mark.asyncio
async def test_an_explicit_success_is_unchanged(handlers):
    result = await execute_tool_call(
        "set_state", {"key": "k", "value": 1, "context_id": str(DEFAULT)}, "u", None
    )

    assert _payload(result) == {"status": "success"}


@pytest.mark.asyncio
async def test_a_seat_refusal_under_the_default_names_its_source(monkeypatch, gate):
    envelope = tools_mod._error_response(
        "feature_not_available", "Feature 'members' is not available.", help="Ask the owner."
    )

    async def deny(_args, _user_id, _workspace_id):
        return envelope

    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", {"recall": deny})
    set_mcp_default_context_selection(URL)

    payload = _payload(await execute_tool_call("recall", {"query": "q"}, "user-1", None))

    assert payload["context_source"] == "url"
    assert payload["help"] == "Ask the owner."
    assert payload["message"] == json.loads(envelope[0].text)["message"]


# ------------------------------------------------------------------- schemas


def test_context_id_is_not_required_where_the_default_applies():
    by_name = {d["name"]: d for d in get_tool_definitions()}
    for name in DEFAULTED:
        schema = by_name[name]["inputSchema"]
        assert "context_id" in schema["properties"], name
        assert "context_id" not in schema.get("required", []), name


def test_context_id_stays_required_where_it_names_the_row():
    by_name = {d["name"]: d for d in get_tool_definitions()}
    for name in ("delete_context", "update_search_config", "update_context", "bind_agent_context"):
        assert "context_id" in by_name[name]["inputSchema"]["required"], name


# -------------------------------------------------------------------- parity


def _db() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _get_db(db: MagicMock):
    async def gen():
        yield db

    return patch("db.base.get_db", new=gen)


@pytest.fixture
def real_registry(monkeypatch, gate):
    """The real handlers; only the rate limit and capacity gate are stubbed."""
    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", None)
    with patch.object(tools_mod, "_check_rate_limit", new=AsyncMock(return_value=(True, 0, 100))):
        yield


async def _both(tool: str, args: dict, target: UUID) -> tuple[dict, dict]:
    """The same call with the target as an explicit id and as the URL default."""
    set_mcp_default_context_selection(None)
    explicit_result = await execute_tool_call(tool, {**args, "context_id": str(target)}, "u", None)
    set_mcp_default_context_selection(DefaultContextSelection("url", target))
    implicit_result = await execute_tool_call(tool, args, "u", None)
    # Both are tool execution errors (CallToolResult.isError), not just the payload.
    assert getattr(explicit_result, "is_error", False) is True
    assert getattr(implicit_result, "is_error", False) is True
    return _payload(explicit_result), _payload(implicit_result)


def _assert_parity(explicit: dict, implicit: dict) -> None:
    assert explicit["error"] == "context_not_found"
    assert implicit["error"] == explicit["error"]
    assert implicit["message"] == explicit["message"]
    assert implicit["context_id"] == explicit["context_id"]
    assert implicit["context_source"] == "url"
    assert "context_source" not in explicit
    rest = {k: v for k, v in implicit.items() if k not in ("context_source", "help")}
    assert rest == {k: v for k, v in explicit.items() if k != "help"}


@pytest.mark.asyncio
async def test_parity_write_to_a_context_the_service_denies(real_registry):
    """Other workspace, private non-creator, not a member: ContextService denies."""
    from utils.exceptions import NotFoundException

    target = uuid4()
    with (
        _get_db(_db()),
        patch("services.context_service.ContextService") as service,
    ):
        service.return_value.get_context = AsyncMock(
            side_effect=NotFoundException("Context", str(target))
        )
        explicit, implicit = await _both("remember", _remember_args(), target)

    _assert_parity(explicit, implicit)


@pytest.mark.asyncio
async def test_parity_write_with_a_workspace_scoped_key_to_another_workspace(real_registry):
    target = uuid4()
    set_mcp_key_workspace_scope(uuid4())
    with (
        _get_db(_db()),
        patch("services.context_service.ContextService") as service,
    ):
        service.return_value.get_context = AsyncMock(
            return_value=SimpleNamespace(id=target, workspace_id=uuid4())
        )
        explicit, implicit = await _both("remember", _remember_args(), target)

    _assert_parity(explicit, implicit)


@pytest.mark.asyncio
async def test_parity_write_through_a_read_only_agent_binding(real_registry):
    target = uuid4()
    set_agent_scope(AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=uuid4()))
    evaluate = AsyncMock(return_value=(False, "read_only"))
    with (
        _get_db(_db()),
        patch("services.context_service.ContextService") as service,
        patch(
            "services.agent_binding_service.AgentBindingService.evaluate_context_access",
            new=evaluate,
        ),
    ):
        service.return_value.get_context = AsyncMock(
            return_value=SimpleNamespace(id=target, workspace_id=uuid4())
        )
        explicit, implicit = await _both("remember", _remember_args(), target)

    _assert_parity(explicit, implicit)
    # The binding gate was asked about the same context both times.
    assert [c.args[1] for c in evaluate.await_args_list] == [target, target]


@pytest.mark.asyncio
async def test_parity_read_of_a_context_the_resolver_denies(real_registry):
    from utils.exceptions import NotFoundException

    target = uuid4()
    resolver = AsyncMock(side_effect=NotFoundException("Context", str(target)))
    with (
        _get_db(_db()),
        patch(
            "services.permission_service.PermissionService.resolve_context_for_workspace_read",
            new=resolver,
        ),
    ):
        explicit, implicit = await _both("get_context_info", {}, target)

    _assert_parity(explicit, implicit)
    # Same resolver call, same arguments, both times.
    first, second = resolver.await_args_list
    assert first.kwargs == second.kwargs
    assert first.kwargs["context_id"] == target


# ------------------------------------------------------- agent default binding


def _agent() -> AgentScope:
    scope = AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=uuid4())
    set_agent_scope(scope)
    return scope


def _bindings(outcome: str, context_id: UUID | None = None):
    """Stub ``resolve_default_binding`` with one outcome."""
    binding = SimpleNamespace(context_id=context_id) if context_id is not None else None
    return patch(
        "services.agent_binding_service.AgentBindingService.resolve_default_binding",
        new=AsyncMock(return_value=(binding, outcome)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["default", "sole"])
async def test_an_agent_key_without_a_url_default_uses_its_binding(handlers, outcome):
    scope = _agent()
    bound = uuid4()
    set_mcp_default_context_selection(DefaultContextSelection("none"))
    with _get_db(_db()), _bindings(outcome, bound) as resolve:
        await execute_tool_call("remember", _remember_args(), "user-1", None)

    assert handlers["remember"]["context_id"] == str(bound)
    assert resolve.await_args.args == (scope.agent_id,)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["none", "ambiguous"])
async def test_no_or_ambiguous_bindings_are_context_id_required_naming_nothing(handlers, outcome):
    _agent()
    with _get_db(_db()), _bindings(outcome):
        result = await execute_tool_call("remember", _remember_args(), "user-1", None)

    payload = _payload(result)
    assert payload["error"] == "context_id_required"
    assert "remember" not in handlers
    # No binding is enumerated or hinted at — the envelope is the no-default one.
    assert set(payload) == {"status", "error", "message", "help", "example"}


@pytest.mark.asyncio
async def test_the_url_beats_the_binding(handlers):
    _agent()
    set_mcp_default_context_selection(URL)
    with _bindings("default", uuid4()) as resolve:
        await execute_tool_call("remember", _remember_args(), "user-1", None)

    assert handlers["remember"]["context_id"] == str(DEFAULT)
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_explicit_id_beats_the_binding_without_a_lookup(handlers):
    _agent()
    explicit = str(uuid4())
    with _bindings("default", uuid4()) as resolve:
        await execute_tool_call("remember", _remember_args(context_id=explicit), "u", None)

    assert handlers["remember"]["context_id"] == explicit
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_user_credential_never_looks_up_a_binding(handlers):
    with _bindings("default", uuid4()) as resolve:
        result = await execute_tool_call("remember", _remember_args(), "user-1", None)

    assert _payload(result)["error"] == "context_id_required"
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_binding_lookup_failure_is_no_default(handlers):
    _agent()
    failing = AsyncMock(side_effect=RuntimeError("db down"))
    with (
        _get_db(_db()),
        patch(
            "services.agent_binding_service.AgentBindingService.resolve_default_binding",
            new=failing,
        ),
    ):
        result = await execute_tool_call("remember", _remember_args(), "user-1", None)

    assert _payload(result)["error"] == "context_id_required"
    assert "remember" not in handlers


@pytest.mark.asyncio
async def test_an_exempt_tool_never_looks_up_a_binding(handlers):
    _agent()
    with _bindings("default", uuid4()) as resolve:
        result = await execute_tool_call("delete_context", {}, "user-1", None)

    assert _payload(result)["error"] == "context_id_required"
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_denied_binding_default_names_the_binding(monkeypatch, gate):
    _agent()
    bound = uuid4()
    envelope = tools_mod._error_response(
        "context_not_found",
        "Context not found or you don't have access to it.",
        context_id=str(bound),
        help="Use list_contexts() to see contexts you have access to.",
    )

    async def deny(_args, _user_id, _workspace_id):
        return envelope

    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", {"get_context_info": deny})
    with _get_db(_db()), _bindings("default", bound):
        payload = _payload(await execute_tool_call("get_context_info", {}, "user-1", None))

    assert payload["context_source"] == "binding"
    assert payload["message"] == json.loads(envelope[0].text)["message"]
    assert "binding" in payload["help"]


@pytest.mark.asyncio
async def test_an_invalid_url_value_never_falls_back_to_the_binding(handlers):
    _agent()
    set_mcp_default_context_selection(DefaultContextSelection("invalid", raw_length=4))
    with _bindings("default", uuid4()) as resolve:
        result = await execute_tool_call("remember", _remember_args(), "user-1", None)

    assert _payload(result)["error"] == "invalid_context_id_format"
    resolve.assert_not_awaited()
    assert "remember" not in handlers
