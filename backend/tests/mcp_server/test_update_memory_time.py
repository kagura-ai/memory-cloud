"""Regression test: MCP handle_update_memory maps a Time Memory trigger
ValueError to a structured validation_error (Issue #877).

When type="time" normalization (MemoryService._apply_time_trigger) is reached via
the update path and the trigger is invalid, the handler must return a structured
validation_error — not re-raise as an opaque tool crash (mirrors handle_remember).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.memory import handle_update_memory


@pytest.mark.asyncio
async def test_handle_update_memory_time_value_error_returns_validation_error():
    mock_db = AsyncMock()

    async def mock_get_db():
        yield mock_db

    mock_service = MagicMock()
    mock_service.update_memory = AsyncMock(
        side_effect=ValueError("invalid details.trigger: trigger.month out of range (1-12)")
    )

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch("mcp_server.tools.memory._check_viewer_permission", new=AsyncMock(return_value=None)),
        patch("mcp_server.tools.memory._resolve_context", new=AsyncMock(return_value=MagicMock())),
        patch("services.memory_service.MemoryService", return_value=mock_service),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
    ):
        result = await handle_update_memory(
            {
                "context_id": str(uuid4()),
                "memory_id": str(uuid4()),
                "type": "time",
                "details": {"trigger": {"year": 2026, "month": 13}},
            },
            user_id="u1",
            workspace_id=None,
        )

    payload = json.loads(result[0].text)
    assert payload["status"] == "error"
    assert payload["error"] == "validation_error"
