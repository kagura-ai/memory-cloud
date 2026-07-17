"""MCP recall maps malformed ``filters`` to validation_error (#1332).

The tool description advertises the free-form ``near`` filter, so a typo
(``radius`` vs ``radius_m``) is routine client input — it must return the
structured validation_error envelope (the REST 422 mirror), not re-raise
through the dispatcher as a 500-shaped tool crash.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.memory import handle_recall
from utils.geo_location import LocationValidationError


@contextmanager
def _patched_recall(service_recall: AsyncMock):
    db = AsyncMock()

    async def mock_get_db():
        yield db

    ctx = SimpleNamespace(
        id=uuid4(), name="ctx", last_used_at=None, workspace_id=uuid4(), is_private=False
    )
    service = MagicMock()
    service.recall = service_recall

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=ctx),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()) as log_mock,
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        yield db, log_mock


@pytest.mark.asyncio
async def test_malformed_near_returns_validation_error_envelope():
    boom = AsyncMock(
        side_effect=LocationValidationError(
            "near has unknown keys: ['radius'] (allowed: lat, lon, radius_m)"
        )
    )
    with _patched_recall(boom) as (db, log_mock):
        result = await handle_recall(
            {
                "query": "cafe",
                "context_id": str(uuid4()),
                "filters": {"near": {"lat": 35.6, "lon": 139.7, "radius": 500}},
            },
            user_id="u1",
            workspace_id=None,
        )

    body = json.loads(result[0].text)
    assert body["status"] == "error"
    assert body["error"] == "validation_error"
    assert "radius" in body["message"]
    # Logged as client input (422), not a server fault (500).
    assert log_mock.await_args.args[4] == 422
    db.rollback.assert_awaited()
