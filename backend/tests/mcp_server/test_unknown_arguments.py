"""Unknown tool arguments are refused, not dropped (#1742).

Every tool's inputSchema sets ``additionalProperties: false``, but
``execute_tool_call`` used to ignore undeclared keys: ``remember`` with
``importanc`` and ``tagz`` succeeded and stored the defaults. The dispatcher
now answers ``invalid_argument`` with the accepted names and a did-you-mean
hint. The deliberate aliases (``merge_contexts``' ``source_id`` /
``target_id``) and ``_meta`` stay accepted.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

import mcp_server.tools as tools_mod
from mcp_server.tools import execute_tool_call, get_tool_definitions
from mcp_server.tools._arg_coercion import (
    _ACCEPTED_ALIASES,
    _CLOSED_TOOLS,
    find_unknown_arguments,
)


@pytest.fixture
def handlers(monkeypatch):
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


def test_every_tool_schema_is_closed():
    """The check covers every tool (and would silently skip an open schema)."""
    assert _CLOSED_TOOLS == {d["name"] for d in get_tool_definitions()}


def test_aliases_name_real_tools():
    names = {d["name"] for d in get_tool_definitions()}
    assert set(_ACCEPTED_ALIASES) <= names


@pytest.mark.asyncio
async def test_a_misspelled_argument_is_refused_with_a_suggestion(handlers):
    result = await execute_tool_call(
        "remember",
        {
            "context_id": str(uuid4()),
            "summary": "a summary long enough",
            "content": "c",
            "type": "note",
            "importanc": 0.9,
            "tagz": ["a"],
        },
        "user-1",
        uuid4(),
    )

    assert getattr(result, "is_error", False) is True
    payload = _payload(result)
    assert payload["error"] == "invalid_argument"
    assert payload["unknown_arguments"] == ["importanc", "tagz"]
    assert payload["suggestions"] == {"importanc": "importance", "tagz": "tags"}
    assert "importance" in payload["allowed_arguments"]
    assert "did you mean 'importance'?" in payload["message"]
    assert payload["help"]
    assert "remember" not in handlers  # the handler never ran


@pytest.mark.asyncio
async def test_an_unrelated_name_gets_no_suggestion(handlers):
    result = await execute_tool_call("list_contexts", {"zzzzzz": 1}, "user-1", uuid4())
    payload = _payload(result)
    assert payload["error"] == "invalid_argument"
    assert payload["suggestions"] == {}
    assert "'zzzzzz'" in payload["message"]


@pytest.mark.asyncio
async def test_declared_arguments_still_pass(handlers):
    args = {"context_id": str(uuid4()), "query": "q", "k": 3}
    result = await execute_tool_call("recall", args, "user-1", uuid4())
    assert _payload(result)["status"] == "success"
    assert handlers["recall"]["query"] == "q"


@pytest.mark.asyncio
async def test_aliases_are_tolerated_but_not_advertised(handlers):
    result = await execute_tool_call(
        "merge_contexts",
        {"source_id": str(uuid4()), "target_id": str(uuid4()), "delete_sourse": True},
        "user-1",
        uuid4(),
    )
    payload = _payload(result)
    assert payload["unknown_arguments"] == ["delete_sourse"]
    assert payload["suggestions"] == {"delete_sourse": "delete_source"}
    assert "source_id" not in payload["allowed_arguments"]
    assert "_meta" not in payload["allowed_arguments"]


@pytest.mark.asyncio
async def test_merge_contexts_deprecated_aliases_are_accepted(handlers):
    args = {"source_id": str(uuid4()), "target_id": str(uuid4())}
    result = await execute_tool_call("merge_contexts", args, "user-1", uuid4())
    assert _payload(result)["status"] == "success"
    assert handlers["merge_contexts"] == args


@pytest.mark.asyncio
async def test_meta_is_accepted_on_any_tool(handlers):
    result = await execute_tool_call(
        "list_contexts", {"_meta": {"progressToken": 1}}, "user-1", uuid4()
    )
    assert _payload(result)["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["context_id=x", 7, ["a"], True])
async def test_non_object_arguments_are_refused(handlers, arguments):
    result = await execute_tool_call("list_contexts", arguments, "user-1", uuid4())
    payload = _payload(result)
    assert payload["error"] == "invalid_argument"
    assert payload["message"] == "'arguments' must be an object."
    assert handlers == {}


@pytest.mark.asyncio
async def test_null_arguments_mean_none(handlers):
    result = await execute_tool_call("list_contexts", None, "user-1", uuid4())
    assert _payload(result)["status"] == "success"


def test_echoed_names_are_bounded():
    unknown = find_unknown_arguments("list_contexts", {f"x{i}" * 50: 1 for i in range(30)})
    assert unknown is not None
    assert len(unknown["unknown_arguments"]) == 10
    assert all(len(name) <= 64 for name in unknown["unknown_arguments"])
    assert "and 20 more" in unknown["message"]


def test_unknown_tool_is_left_to_the_unknown_tool_refusal():
    assert find_unknown_arguments("no_such_tool", {"x": 1}) is None
