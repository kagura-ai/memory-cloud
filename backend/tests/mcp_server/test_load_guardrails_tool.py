"""Tests for the MCP ``load_guardrails`` tool — handler envelope and registry wiring.

The service contract is pinned in ``tests/services/test_load_guardrails_service.py``
and the SQL in ``tests/integration/test_load_guardrails_repo.py``. Here:

* the handler's envelope (``format`` / ``version`` / both lists / per-lane
  flags / context fields), the ``missing_fields`` and uniform
  ``context_not_found`` errors, ``validation_error`` on a bad cap;
* the registry frozensets (the "new MCP tool = up to 3 frozensets" trap):
  registered, rate-limit exempt, NOT in ``_TOOLS_WITHOUT_CONTEXT_ID``, NOT in
  ``CORE_TOOLS`` (hooks call it through ``tools/call``; a profile is a view);
* the definition: ``readOnly``, inserted right after ``load_pinned``, within
  the per-tool budget;
* the write handlers map the guardrail author gate's ``AuthorizationError``
  to a ``permission_denied`` envelope (flagged ``is_error``).
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools import (
    _RATE_LIMIT_EXEMPT_TOOLS,
    _TOOLS_WITHOUT_CONTEXT_ID,
    _build_registry,
    get_tool_definitions,
)
from mcp_server.tools._helpers import ToolErrorContent, _ContextNotFoundError
from mcp_server.tools._profiles import CORE_TOOLS
from mcp_server.tools.memory import (
    handle_load_guardrails,
    handle_remember,
    handle_update_memory,
)
from models.schemas import GuardrailItem, LoadGuardrailsResponse
from utils.exceptions import AuthorizationError

NOW = datetime(2026, 9, 22, 9, 0, 0, tzinfo=UTC)
TT = {"tool": "Bash|PowerShell", "on": "pre", "match": "gh pr merge", "action": "block"}


def _item(**o) -> GuardrailItem:
    return GuardrailItem(
        memory_id=o.get("memory_id", uuid4()),
        summary=o.get("summary", "remove the worktree before merging"),
        context_summary=o.get("context_summary"),
        type="troubleshooting",
        importance=0.8,
        delivery_mode=o.get("delivery_mode", "on_recall"),
        tool_trigger=o.get("tool_trigger"),
        source_type="manual",
        authored_by_caller=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _response(pinned=(), tool=(), **flags) -> LoadGuardrailsResponse:
    base = {
        "format": 1,
        "version": "3f9c1a7b2d4e6f80",
        "pinned": list(pinned),
        "tool_triggered": list(tool),
        "total_available": len(pinned) + len(tool),
        "truncated": False,
        "cap": 50,
        "pinned_cap": 100,
        "pinned_total_available": len(pinned),
        "pinned_truncated": False,
        "tool_triggered_total_available": len(tool),
        "tool_triggered_truncated": False,
    }
    base.update(flags)
    return LoadGuardrailsResponse(**base)


def _get_db_yielding(db):
    async def _get_db():
        yield db

    return _get_db


@contextlib.contextmanager
def _patched(service, *, resolve=None):
    db = AsyncMock()
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.name = "kagura-dev"
    ctx.display_name = "Kagura Dev"
    ctx.is_private = False
    ctx.is_locked = False
    with (
        patch("db.base.get_db", new=_get_db_yielding(db)),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=resolve or AsyncMock(return_value=ctx),
        ),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()) as log,
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        yield ctx, log


# ------------------------------------------------------------------- envelope


@pytest.mark.asyncio
async def test_success_envelope_carries_format_version_both_lists_and_flags():
    shared = uuid4()
    pinned = _item(memory_id=shared, delivery_mode="always", context_summary="L2 for pinned")
    tool = _item(memory_id=shared, delivery_mode="always", tool_trigger=dict(TT))
    service = MagicMock()
    service.load_guardrails = AsyncMock(return_value=_response([pinned], [tool]))

    with _patched(service) as (ctx, log):
        result = await handle_load_guardrails(
            {"context_id": str(ctx.id), "cap": 7}, user_id="u1", workspace_id=None
        )

    payload = json.loads(result[0].text)
    assert payload["status"] == "success"
    assert payload["format"] == 1
    assert payload["version"] == "3f9c1a7b2d4e6f80"
    assert [i["memory_id"] for i in payload["pinned"]] == [str(shared)]
    assert [i["memory_id"] for i in payload["tool_triggered"]] == [str(shared)]
    # Item shape — one shape for both lists; L2 on pinned only; UTC Z timestamps.
    p, t = payload["pinned"][0], payload["tool_triggered"][0]
    assert set(p) == {
        "memory_id",
        "summary",
        "context_summary",
        "type",
        "importance",
        "delivery_mode",
        "tool_trigger",
        "source_type",
        "authored_by_caller",
        "created_at",
        "updated_at",
    }
    assert p["tool_trigger"] is None and p["context_summary"] == "L2 for pinned"
    assert t["tool_trigger"] == TT and t["context_summary"] is None
    assert t["created_at"] == "2026-09-22T09:00:00Z"
    for key in (
        "total_available",
        "truncated",
        "cap",
        "pinned_cap",
        "pinned_total_available",
        "pinned_truncated",
        "tool_triggered_total_available",
        "tool_triggered_truncated",
    ):
        assert key in payload
    assert payload["context_id"] == str(ctx.id)
    assert payload["context_name"] == "kagura-dev"
    assert "content" not in json.dumps(payload)
    # cap is forwarded raw — the service is the clamp chokepoint.
    assert service.load_guardrails.await_args.kwargs["cap"] == 7
    log.assert_awaited_once()
    assert log.await_args.args[4] == 200


@pytest.mark.asyncio
async def test_missing_context_id_is_a_structured_error():
    result = await handle_load_guardrails({}, user_id="u1", workspace_id=None)
    assert isinstance(result, ToolErrorContent)
    payload = json.loads(result[0].text)
    assert payload["error"] == "missing_fields"


@pytest.mark.asyncio
async def test_context_deny_is_the_uniform_not_found_envelope():
    ctx_id = uuid4()
    service = MagicMock()
    service.load_guardrails = AsyncMock()
    deny = AsyncMock(side_effect=_ContextNotFoundError(ctx_id, "Context not found or no access."))
    with _patched(service, resolve=deny) as (_, log):
        result = await handle_load_guardrails(
            {"context_id": str(ctx_id)}, user_id="u1", workspace_id=None
        )
    payload = json.loads(result[0].text)
    assert payload["status"] == "error"
    assert payload["error"] == "context_not_found"
    service.load_guardrails.assert_not_awaited()
    assert log.await_args.args[4] == 404
    # The pre-gate threads the audit identity of this surface.
    assert deny.await_args.kwargs["operation"] == "load_guardrails"


@pytest.mark.asyncio
async def test_service_value_error_maps_to_validation_error_and_logs_422():
    service = MagicMock()
    service.load_guardrails = AsyncMock(side_effect=ValueError("cap must be an integer, got 'x'"))
    with _patched(service) as (ctx, log):
        result = await handle_load_guardrails(
            {"context_id": str(ctx.id), "cap": "x"}, user_id="u1", workspace_id=None
        )
    payload = json.loads(result[0].text)
    assert payload["error"] == "validation_error"
    assert "cap" in payload["message"]
    assert log.await_args.args[4] == 422


# ------------------------------------------------------------------- registry


def _tool(name):
    return next(t for t in get_tool_definitions() if t["name"] == name)


def test_registered_exempt_and_context_scoped():
    assert _build_registry()["load_guardrails"] is handle_load_guardrails
    assert "load_guardrails" in _RATE_LIMIT_EXEMPT_TOOLS
    assert "load_guardrails" not in _TOOLS_WITHOUT_CONTEXT_ID


def test_not_in_the_core_profile():
    """Hooks call it through tools/call, which ignores the profile; a core
    client's model has no reason to call it, so it does not pay for the schema."""
    assert "load_guardrails" not in CORE_TOOLS


def test_definition_is_read_only_and_sits_after_load_pinned():
    names = [t["name"] for t in get_tool_definitions()]
    assert names.index("load_guardrails") == names.index("load_pinned") + 1
    tool = _tool("load_guardrails")
    assert tool["readOnly"] is True
    assert tool["inputSchema"]["required"] == ["context_id"]
    assert set(tool["inputSchema"]["properties"]) == {"context_id", "cap"}
    assert tool["inputSchema"]["additionalProperties"] is False


def test_definition_stays_small():
    size = len(json.dumps(_tool("load_guardrails"), ensure_ascii=False, separators=(",", ":")))
    assert size <= 1_900, size


def test_definition_names_the_contract_an_agent_must_not_lose():
    text = _tool("load_guardrails")["description"]
    for term in ("trusted-tier", "never runs them", "both lists", "cap bounds tool_triggered"):
        assert term in text, term


def test_remember_details_description_points_at_tool_trigger():
    details = _tool("remember")["inputSchema"]["properties"]["details"]["description"]
    assert "tool_trigger" in details


# ------------------------------------------------- write handlers: author gate


@contextlib.contextmanager
def _patched_write(service):
    db = AsyncMock()
    with (
        patch("db.base.get_db", new=_get_db_yielding(db)),
        patch("mcp_server.tools.memory._check_viewer_permission", new=AsyncMock(return_value=None)),
        patch("mcp_server.tools.memory._resolve_context", new=AsyncMock(return_value=MagicMock())),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()) as log,
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        yield db, log


@pytest.mark.asyncio
async def test_remember_maps_author_gate_to_permission_denied():
    service = MagicMock()
    service.remember = AsyncMock(side_effect=AuthorizationError())
    with _patched_write(service) as (db, log):
        result = await handle_remember(
            {
                "context_id": str(uuid4()),
                "summary": "a guardrail summary",
                "content": "c",
                "type": "troubleshooting",
                "details": {"tool_trigger": TT},
            },
            user_id="viewer",
            workspace_id=None,
        )
    assert isinstance(result, ToolErrorContent)
    payload = json.loads(result[0].text)
    assert payload["error"] == "permission_denied"
    assert payload["required_role"] == "editor"
    assert "Insufficient" not in payload["message"]  # our own uniform wording, no sub-reason
    db.rollback.assert_awaited()
    assert log.await_args.args[4] == 403


@pytest.mark.asyncio
async def test_update_memory_maps_author_gate_to_permission_denied():
    service = MagicMock()
    service.update_memory = AsyncMock(side_effect=AuthorizationError())
    with _patched_write(service) as (db, log):
        result = await handle_update_memory(
            {"context_id": str(uuid4()), "memory_id": str(uuid4()), "summary": "rewrite it"},
            user_id="member",
            workspace_id=None,
        )
    payload = json.loads(result[0].text)
    assert payload["error"] == "permission_denied"
    assert log.await_args.args[4] == 403
