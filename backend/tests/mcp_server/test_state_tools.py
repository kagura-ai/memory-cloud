"""Unit tests for the agent session-state MCP handlers (Issue #889).

Pins the access-control composition (IDOR guard + write gate) and the
arg/dispatch contract of ``handle_set_state`` / ``handle_get_state`` without a
database — the service is mocked. The DB-backed service behaviour (upsert, TTL
expiry, list, delete) is covered in
tests/integration/test_agent_state_service.py.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._helpers import _ContextNotFoundError, _error_response
from mcp_server.tools.state import handle_get_state, handle_set_state

CTX = str(uuid4())


def _payload(result):
    assert len(result) == 1
    return json.loads(result[0].text)


def _enter(stack, *, service, resolve_raises=None, viewer_error=None):
    """Enter the standard patch set and return the mocked service."""

    async def gen():
        yield AsyncMock()

    resolve = AsyncMock(side_effect=resolve_raises) if resolve_raises else AsyncMock()
    stack.enter_context(patch("db.base.get_db", new=gen))
    stack.enter_context(patch("mcp_server.tools.state._resolve_context_for_read", new=resolve))
    stack.enter_context(
        patch(
            "mcp_server.tools.state._check_viewer_permission",
            new=AsyncMock(return_value=viewer_error),
        )
    )
    stack.enter_context(
        patch("services.agent_state_service.AgentStateService", return_value=service)
    )
    return service


class TestSetState:
    @pytest.mark.asyncio
    async def test_missing_fields_returns_error(self):
        for args in ({"context_id": CTX, "key": "k"}, {"context_id": CTX, "value": 1}):
            result = await handle_set_state(args=args, user_id="u", workspace_id=uuid4())
            assert _payload(result)["error"] == "missing_fields"

    @pytest.mark.asyncio
    async def test_context_not_found_short_circuits(self):
        svc = MagicMock(set_state=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc, resolve_raises=_ContextNotFoundError(uuid4(), "nope"))
            result = await handle_set_state(
                args={"context_id": CTX, "key": "k", "value": 1},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "context_not_found"
        svc.set_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_viewer_is_blocked_from_writing(self):
        svc = MagicMock(set_state=AsyncMock())
        blocked = _error_response("permission_denied", "viewers cannot write")
        with ExitStack() as stack:
            _enter(stack, service=svc, viewer_error=blocked)
            result = await handle_set_state(
                args={"context_id": CTX, "key": "k", "value": 1},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "permission_denied"
        svc.set_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_passes_ttl_through(self):
        svc = MagicMock(set_state=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_set_state(
                args={
                    "context_id": CTX,
                    "key": "task",
                    "value": {"step": 2},
                    "ttl_seconds": 60,
                },
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["status"] == "ok"
        svc.set_state.assert_awaited_once()
        assert svc.set_state.call_args.kwargs["ttl_seconds"] == 60


class TestGetState:
    @pytest.mark.asyncio
    async def test_single_key_returns_value_and_found(self):
        svc = MagicMock(get_state=AsyncMock(return_value={"step": 2}))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_get_state(
                args={"context_id": CTX, "key": "task"}, user_id="u", workspace_id=uuid4()
            )
        body = _payload(result)
        assert body["found"] is True
        assert body["value"] == {"step": 2}

    @pytest.mark.asyncio
    async def test_missing_key_reports_not_found(self):
        svc = MagicMock(get_state=AsyncMock(return_value=None))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_get_state(
                args={"context_id": CTX, "key": "absent"}, user_id="u", workspace_id=uuid4()
            )
        body = _payload(result)
        assert body["found"] is False
        assert body["value"] is None

    @pytest.mark.asyncio
    async def test_no_key_lists_all_live_entries(self):
        svc = MagicMock(list_state=AsyncMock(return_value={"a": 1, "b": 2}))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_get_state(
                args={"context_id": CTX}, user_id="u", workspace_id=uuid4()
            )
        body = _payload(result)
        assert body["count"] == 2
        assert body["states"] == {"a": 1, "b": 2}

    @pytest.mark.asyncio
    async def test_context_not_found_short_circuits(self):
        svc = MagicMock(get_state=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc, resolve_raises=_ContextNotFoundError(uuid4(), "nope"))
            result = await handle_get_state(
                args={"context_id": CTX, "key": "task"}, user_id="u", workspace_id=uuid4()
            )
        assert _payload(result)["error"] == "context_not_found"
        svc.get_state.assert_not_called()
