"""``update_memory(accept_supersede_candidate=true)`` at the MCP boundary (#2308).

The service behaviour runs on Postgres in
``tests/integration/test_2308_accept_supersede_candidate_db.py``; this pins
the tool's wiring: the flag reaches the service, the accepted target is
echoed as ``superseded_memory_id``, the refusals keep the existing envelopes
and a viewer is turned away exactly as ``create_edge`` turns one away.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.edge import handle_create_edge
from mcp_server.tools.memory import handle_update_memory
from utils.exceptions import SupersedeEdgeConflictError


def _result(**overrides):
    fields = {
        "memory_id": uuid4(),
        "operation": "updated",
        "re_embedded": False,
        "scope": "persistent",
        "version": 2,
        "persistence": None,
        "supersede_candidate_dismissed": None,
        "superseded_memory_id": None,
        "lint": [],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@contextlib.contextmanager
def _patched(service, *, role=None):
    db = AsyncMock()

    async def mock_get_db():
        yield db

    context = SimpleNamespace(
        id=uuid4(), name="ctx", last_used_at=None, workspace_id=uuid4(), is_private=False
    )
    with (
        patch("db.base.get_db", new=mock_get_db),
        patch("mcp_server.tools.memory._resolve_context", new=AsyncMock(return_value=context)),
        patch("mcp_server.tools.edge._resolve_context", new=AsyncMock(return_value=context)),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("mcp_server.tools.edge._log_tool_usage", new=AsyncMock()),
        patch(
            "mcp_server.tools._helpers._get_workspace_member_role",
            new=AsyncMock(return_value=role),
        ),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        yield db


async def _call(service, args, *, role=None, workspace_id=None):
    with _patched(service, role=role):
        result = await handle_update_memory(
            {"context_id": str(uuid4()), **args}, user_id="u1", workspace_id=workspace_id
        )
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_the_flag_reaches_the_service_and_the_target_is_echoed():
    target = uuid4()
    service = MagicMock()
    service.update_memory = AsyncMock(return_value=_result(superseded_memory_id=target))

    body = await _call(service, {"memory_id": str(uuid4()), "accept_supersede_candidate": True})

    assert body["status"] == "success"
    assert body["superseded_memory_id"] == str(target)
    request = service.update_memory.await_args.args[0]
    assert request.accept_supersede_candidate is True
    assert request.dismiss_supersede_candidate is False


@pytest.mark.asyncio
async def test_superseded_memory_id_is_absent_on_an_ordinary_edit():
    service = MagicMock()
    service.update_memory = AsyncMock(return_value=_result())

    body = await _call(
        service, {"memory_id": str(uuid4()), "summary": "an ordinary edit, long enough"}
    )

    assert body["status"] == "success"
    assert "superseded_memory_id" not in body
    assert service.update_memory.await_args.args[0].accept_supersede_candidate is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"dismiss_supersede_candidate": True},
        {"summary": "an edit in the same call"},
        {"tags": ["x"]},
    ],
)
async def test_conflicting_arguments_are_a_validation_error(extra):
    service = MagicMock()
    service.update_memory = AsyncMock()

    body = await _call(
        service,
        {"memory_id": str(uuid4()), "accept_supersede_candidate": True, **extra},
    )

    assert body["status"] == "error"
    assert body["error"] == "validation_error"
    assert "accept_supersede_candidate" in body["message"]
    service.update_memory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["null", "no", 1])
async def test_a_non_boolean_flag_fails_closed(value):
    """Like create_edge's overwrite: an uncoerced value never writes an edge."""
    service = MagicMock()
    service.update_memory = AsyncMock()

    body = await _call(service, {"memory_id": str(uuid4()), "accept_supersede_candidate": value})

    assert body["error"] == "validation_error"
    assert "accept_supersede_candidate must be a boolean" in body["message"]
    service.update_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_candidate_is_a_validation_error_with_the_hint():
    service = MagicMock()
    service.update_memory = AsyncMock(
        side_effect=ValueError(
            "This memory has no supersede_candidate to accept; recall() or reference() it "
            "to see its current suggestion."
        )
    )

    body = await _call(service, {"memory_id": str(uuid4()), "accept_supersede_candidate": True})

    assert body["error"] == "validation_error"
    assert "no supersede_candidate" in body["message"]


@pytest.mark.asyncio
async def test_a_conflicting_declared_edge_is_edge_exists_like_create_edge():
    existing = {"source_id": "a", "target_id": "b", "edge_type": "related_to"}
    service = MagicMock()
    service.update_memory = AsyncMock(
        side_effect=SupersedeEdgeConflictError(source_id="a", target_id="b", existing=existing)
    )

    body = await _call(service, {"memory_id": str(uuid4()), "accept_supersede_candidate": True})

    assert body["error"] == "edge_exists"
    assert body["existing_edge"] == existing


@pytest.mark.asyncio
async def test_a_viewer_is_refused_as_create_edge_refuses_one():
    service = MagicMock()
    service.update_memory = AsyncMock()
    workspace_id = uuid4()

    accept = await _call(
        service,
        {"memory_id": str(uuid4()), "accept_supersede_candidate": True},
        role="viewer",
        workspace_id=workspace_id,
    )
    with _patched(service, role="viewer"):
        edge = json.loads(
            (
                await handle_create_edge(
                    {
                        "context_id": str(uuid4()),
                        "source_id": str(uuid4()),
                        "target_id": str(uuid4()),
                        "edge_type": "supersedes",
                    },
                    user_id="u1",
                    workspace_id=workspace_id,
                )
            )[0].text
        )

    assert accept["error"] == edge["error"] == "permission_denied"
    assert accept["your_role"] == edge["your_role"] == "viewer"
    service.update_memory.assert_not_awaited()
