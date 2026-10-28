"""#2313: denied and stalled owner checks have normal MCP failure handling."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.edge import handle_create_edge, handle_update_edge
from utils.exceptions import AuthorizationError


@pytest.fixture
def edge_environment():
    db = AsyncMock()
    context = SimpleNamespace(id=uuid4(), workspace_id=uuid4())
    repo = MagicMock()
    repo.get_edge = AsyncMock(return_value=SimpleNamespace(edge_type="related_to"))
    repo.create_or_update_edge = AsyncMock()
    usage = AsyncMock()

    async def get_db():
        yield db

    args = {
        "context_id": str(context.id),
        "source_id": str(uuid4()),
        "target_id": str(uuid4()),
        "edge_type": "supersedes",
    }
    with (
        patch("db.base.get_db", get_db),
        patch("mcp_server.tools.edge._resolve_context", AsyncMock(return_value=context)),
        patch("mcp_server.tools.edge.plan_seats.ensure_context_writable", AsyncMock()),
        patch("repositories.neural_edge.NeuralEdgeRepository", return_value=repo),
        patch("mcp_server.tools.edge._log_tool_usage", usage),
    ):
        yield db, repo, usage, context, args


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["create_edge", "update_edge"])
async def test_owner_denial_is_logged_as_403_after_rollback(edge_environment, tool):
    db, repo, usage, context, args = edge_environment
    events = []
    db.rollback.side_effect = lambda: events.append("rollback")
    usage.side_effect = lambda *a, **kw: events.append("log")
    handler = handle_create_edge if tool == "create_edge" else handle_update_edge
    with (
        patch(
            "services.edge_service.ensure_supersedes_owned",
            AsyncMock(side_effect=AuthorizationError),
        ),
        patch(
            "mcp_server.tools.edge.ensure_supersedes_owned",
            AsyncMock(side_effect=AuthorizationError),
        ),
    ):
        response = await handler(args, "caller", context.workspace_id)

    assert json.loads(response[0].text)["error"] == "permission_denied"
    assert events == ["rollback", "log"]
    usage.assert_awaited_once()
    assert usage.await_args.args[2] == tool
    assert usage.await_args.args[4:] == (403, context.id, context.workspace_id)
    repo.create_or_update_edge.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_owner_check_uses_tool_timeout_and_rolls_back(edge_environment):
    db, repo, usage, context, args = edge_environment
    cancelled = asyncio.Event()

    async def blocked(*a, **kw):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with (
        patch("mcp_server.tools.edge.ensure_supersedes_owned", blocked),
        patch("mcp_server.tools._helpers.get_tool_timeout", return_value=0.01) as timeout,
        pytest.raises(TimeoutError),
    ):
        # Outer guard also bounds the broken implementation (no inner timeout).
        await asyncio.wait_for(handle_update_edge(args, "caller", context.workspace_id), 0.3)

    timeout.assert_called_once_with("update_edge")
    assert cancelled.is_set()
    db.rollback.assert_awaited_once()
    repo.create_or_update_edge.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert usage.await_args.args[4] == 500
