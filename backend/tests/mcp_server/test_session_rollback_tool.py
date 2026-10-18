"""The ``rollback_session`` MCP handler (#2044): arguments, authorization and envelopes.

The service is stubbed; its planner / executor are covered in
tests/services/test_session_rollback_planner.py and on Postgres in
tests/integration/test_session_rollback_db.py (cross-principal included).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import ContextRole
from mcp_server.tools import session_rollback as tool
from services import session_rollback as sr

CTX = uuid.UUID("00000000-0000-4000-8000-000000000044")
WS = uuid.uuid4()


async def _call(args: dict[str, Any], **patches: Any) -> tuple[dict[str, Any], MagicMock]:
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def get_db() -> AsyncIterator[Any]:
        yield db

    defaults: dict[str, Any] = {
        "role": ContextRole.EDITOR,
        "result": sr.RollbackResult(dry_run=True, items=[], events=0, remaining=0),
    }
    defaults.update(patches)
    service = AsyncMock(side_effect=defaults["result"])
    if not isinstance(defaults["result"], BaseException):
        service = AsyncMock(return_value=defaults["result"])
    with (
        patch("db.base.get_db", get_db),
        patch.object(tool, "_check_viewer_permission", AsyncMock(return_value=None)),
        patch.object(
            tool,
            "_resolve_context",
            AsyncMock(return_value=MagicMock(id=CTX, workspace_id=WS)),
        ),
        patch.object(tool, "_context_response_fields", lambda _c: {"context_id": str(CTX)}),
        patch.object(tool, "_log_tool_usage", AsyncMock()),
        patch.object(sr, "context_role", AsyncMock(return_value=defaults["role"])),
        patch.object(sr, "rollback_session", service),
        patch.object(sr, "finish", AsyncMock()) as finish,
    ):
        (block,) = await tool.handle_rollback_session(
            {"context_id": str(CTX), **args}, "caller", WS
        )
        db.finish = finish
        db.service = service
    return json.loads(block.text), db


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({}, "missing_fields"),
        ({"session_id": ""}, "missing_fields"),
        ({"session_id": "has space"}, "validation_error"),
        ({"session_id": "x" * 129}, "validation_error"),
        ({"session_id": "s", "dry_run": "false"}, "validation_error"),
    ],
)
async def test_arguments_are_checked_before_the_database(args, error):
    (block,) = await tool.handle_rollback_session({"context_id": str(CTX), **args}, "u", None)
    payload = json.loads(block.text)
    assert payload["status"] == "error" and payload["error"] == error


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [ContextRole.VIEWER, None])
async def test_below_editor_is_refused(role):
    payload, db = await _call({"session_id": "s"}, role=role)
    assert payload["error"] == "permission_denied"
    assert payload["required_role"] == "editor"
    db.service.assert_not_awaited()


@pytest.mark.asyncio
async def test_principal_is_the_caller_unless_a_user_credential_owner():
    _, db = await _call({"session_id": "s"}, role=ContextRole.EDITOR)
    scope = db.service.await_args.kwargs["scope"]
    assert scope == sr.SessionScope(CTX, "s", sr.SessionPrincipal("caller", None))

    _, db = await _call({"session_id": "s"}, role=ContextRole.OWNER)
    assert db.service.await_args.kwargs["scope"].principal is None

    agent = uuid.uuid4()
    set_agent_scope(AgentScope(agent_id=agent, enforcement_mode="enforce"))
    try:
        _, db = await _call({"session_id": "s"}, role=ContextRole.OWNER)
    finally:
        set_agent_scope(None)
    assert db.service.await_args.kwargs["scope"].principal == sr.SessionPrincipal("caller", agent)
    assert db.service.await_args.kwargs["dry_run"] is True  # the default


@pytest.mark.asyncio
async def test_another_principals_session_is_not_found():
    payload, db = await _call({"session_id": "theirs"}, result=sr.SessionNotFoundError())
    assert payload["error"] == "session_not_found"
    assert payload["session_id"] == "theirs"
    assert "rollback" not in payload  # nothing about whether it exists elsewhere
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_finished_rollback_is_refused():
    payload, db = await _call(
        {"session_id": "s", "dry_run": False},
        result=sr.AlreadyRolledBackError(5, "completed"),
    )
    assert payload["error"] == "already_rolled_back"
    assert payload["retryable"] is False
    assert (payload["rollback_id"], payload["rollback_status"]) == (5, "completed")
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_lists_the_plan_and_commits_nothing():
    mid = uuid.uuid4()
    result = sr.RollbackResult(
        dry_run=True,
        items=[
            sr.PlanItem(mid, "created", sr.ACTION_FORGET, event_ids=(1,)),
            sr.PlanItem(uuid.uuid4(), "updated", sr.ACTION_SKIP, cause="modified_since"),
        ],
        events=2,
        remaining=0,
    )
    payload, db = await _call({"session_id": "s"}, result=result)
    assert payload["status"] == "dry_run" and payload["dry_run"] is True
    assert payload["plan"] == {"forget": 1, "restore": 0, "unforget": 0, "skip": 1}
    assert payload["items"][0] == {"memory_id": str(mid), "kind": "created", "action": "forget"}
    assert payload["items"][1]["cause"] == "modified_since"
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited()
    db.finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_commits_then_finishes():
    result = sr.RollbackResult(
        dry_run=False,
        items=[],
        events=3,
        remaining=7,
        rollback_id=9,
        status="in_progress",
        summary={"forgotten": 3, "errors": []},
        totals={"forgotten": 3, "calls": 1},
    )
    payload, db = await _call({"session_id": "s", "dry_run": False}, result=result)
    assert payload["status"] == "in_progress" and payload["remaining"] == 7
    assert payload["rollback_id"] == 9 and payload["rollback_status"] == "in_progress"
    db.commit.assert_awaited_once()
    db.finish.assert_awaited_once_with(result, db)
    # v0.113.0 review F2: the call is time-boxed at half the tool timeout.
    from mcp_server.tools._constants import get_tool_timeout

    budget = db.service.await_args.kwargs["time_budget"]
    assert budget == get_tool_timeout("rollback_session") * 0.5


@pytest.mark.asyncio
async def test_failed_items_answer_partial_rollback():
    result = sr.RollbackResult(
        dry_run=False,
        items=[],
        events=2,
        remaining=0,
        rollback_id=9,
        status="partial",
        summary={"failed": 1, "errors": ["Memory x (restore) failed: internal_error"]},
        totals={"failed": 1},
        errors=["Memory x (restore) failed: internal_error"],
        correlation_id="cid",
    )
    payload, db = await _call({"session_id": "s", "dry_run": False}, result=result)
    assert payload["status"] == "error" and payload["error"] == "partial_rollback"
    # The failed items are retried by the next call; nothing is reversed twice.
    assert payload["retryable"] is True and payload["correlation_id"] == "cid"
    assert "again" in payload["help"]
    assert payload["rollback_status"] == "partial"
    assert payload["summary"]["failed"] == 1
    db.commit.assert_awaited_once()  # what did succeed is saved


def test_registration():
    from mcp_server.tools import _TOOLS_WITHOUT_CONTEXT_ID
    from mcp_server.tools._annotations import TOOL_ANNOTATIONS
    from mcp_server.tools._capacity_gate import CAPACITY_LOCK_BLOCKED_TOOLS
    from mcp_server.tools._constants import get_tool_timeout
    from mcp_server.tools._definitions import get_tool_definitions
    from mcp_server.tools._profiles import CORE_TOOLS

    (definition,) = [t for t in get_tool_definitions() if t["name"] == "rollback_session"]
    assert definition["inputSchema"]["required"] == ["context_id", "session_id"]
    annotations = TOOL_ANNOTATIONS["rollback_session"]
    assert annotations["destructiveHint"] is True and annotations["readOnlyHint"] is False
    assert "rollback_session" in CAPACITY_LOCK_BLOCKED_TOOLS
    assert "rollback_session" not in CORE_TOOLS
    assert "rollback_session" not in _TOOLS_WITHOUT_CONTEXT_ID
    assert get_tool_timeout("rollback_session") == 120.0


def test_write_tools_echo_the_recorded_session():
    """A client without baggage learns its MCP session id from its writes."""
    from api.correlation import CorrelationContext, set_correlation
    from mcp_server.tools._definitions import get_tool_definitions
    from mcp_server.tools._helpers import _session_response_field

    set_correlation(None)
    assert _session_response_field() == {}
    set_correlation(
        CorrelationContext(trace_id="0" * 31 + "1", span_id="0" * 15 + "1", session_id="mcp-1")
    )
    try:
        assert _session_response_field() == {"session_id": "mcp-1"}
    finally:
        set_correlation(None)
    returns = {t["name"]: t["description"] for t in get_tool_definitions()}
    for name in ("remember", "remember_batch", "update_memory", "forget"):
        assert "session_id?" in returns[name], name
