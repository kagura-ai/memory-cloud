"""MCP error-envelope consistency (#1323).

Pins three contracts:
- pydantic ValidationError from tool-request construction surfaces as a
  structured ``invalid_argument`` envelope (no pydantic internals/URLs)
  via the dispatch-level arm in ``execute_tool_call``;
- ``_format_validation_error`` renders field/constraint summaries;
- ``handle_update_memory`` returns the ``memory_not_found`` envelope
  (slug + help) instead of falling through to the generic handler.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mcp_server.tools._helpers import _format_validation_error


def _build_validation_error() -> ValidationError:
    from models.schemas import RememberRequest

    try:
        RememberRequest(
            summary="a summary long enough",
            content="c",
            type="note",
            importance=1.5,
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


class TestFormatValidationError:
    def test_names_field_and_constraint_without_pydantic_internals(self):
        message = _format_validation_error(_build_validation_error())

        assert "importance" in message
        assert "less than or equal to 1" in message
        assert "pydantic" not in message.lower()
        assert "RememberRequest" not in message

    def test_non_pydantic_exception_falls_back_to_str(self):
        assert _format_validation_error(RuntimeError("boom")) == "boom"


class TestDispatchValidationArm:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            (
                "remember",
                {
                    "summary": "a summary long enough",
                    "content": "c",
                    "type": "note",
                    "importance": 1.5,
                },
            ),
            ("recall", {"query": "q", "k": 0}),
        ],
    )
    async def test_out_of_range_input_returns_invalid_argument(self, tool, args):
        """#1323: client typos must not leak the raw pydantic dump."""
        from mcp_server.tools import execute_tool_call

        with patch(
            "mcp_server.tools._check_rate_limit",
            new_callable=AsyncMock,
            return_value=(True, 0, 100),
        ):
            result = await execute_tool_call(
                tool,
                {"context_id": str(uuid4()), **args},
                "test_user",
                uuid4(),
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "invalid_argument"
        assert "pydantic" not in payload["message"].lower()
        assert "https://" not in payload["message"]


class TestUpdateMemoryNotFoundEnvelope:
    @pytest.mark.asyncio
    async def test_missing_memory_returns_structured_envelope(self):
        """#1323: update_memory mirrors handle_reference's memory_not_found."""
        from mcp_server.tools.memory import handle_update_memory
        from utils.exceptions import NotFoundException

        mock_db = MagicMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        memory_id = uuid4()
        mock_service = MagicMock()
        mock_service.update_memory = AsyncMock(
            side_effect=NotFoundException("Memory", str(memory_id))
        )

        mock_ctx = MagicMock()
        mock_ctx.id = uuid4()

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.memory._check_viewer_permission",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "mcp_server.tools.memory._resolve_context",
                new=AsyncMock(return_value=mock_ctx),
            ),
            patch(
                "services.memory_service.MemoryService",
                new=MagicMock(return_value=mock_service),
            ),
        ):
            result = await handle_update_memory(
                {
                    "context_id": str(uuid4()),
                    "memory_id": str(memory_id),
                    "importance": 0.9,
                },
                "test_user",
                uuid4(),
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "memory_not_found"
        assert str(memory_id) in payload["message"]
        assert "help" in payload
