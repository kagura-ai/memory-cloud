"""``observed_at`` on remember / remember_batch / update_memory (#2330).

The top-level argument reaches the service folded into ``details``; a
conflicting or malformed value comes back as ``validation_error``; the
remember and update_memory definitions declare it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools.batch import _request_from_item
from mcp_server.tools.memory import remember_request_from_args
from tests.mcp_server.test_value_error_arms import (
    REMEMBER_ARGS,
    _mock_db,
    _payload,
    _run,
)

_CTX = str(uuid4())


def _definition(name: str) -> dict:
    return next(tool for tool in get_tool_definitions() if tool["name"] == name)


class TestDefinitions:
    @pytest.mark.parametrize("name", ["remember", "update_memory"])
    def test_declared(self, name):
        prop = _definition(name)["inputSchema"]["properties"]["observed_at"]
        assert prop["type"] == "string"
        assert "ISO 8601" in prop["description"]


class TestRememberArgs:
    def test_folded_into_details(self):
        request = remember_request_from_args(
            {**REMEMBER_ARGS, "details": {"k": 1}, "observed_at": "2024-03-07"}
        )
        assert request.details == {"k": 1, "observed_at": "2024-03-07"}

    def test_a_batch_item_takes_it(self):
        parsed = _request_from_item({**REMEMBER_ARGS, "observed_at": "2024-03-07"}, _CTX)
        assert not isinstance(parsed, dict), parsed
        assert parsed.request.details == {"observed_at": "2024-03-07"}

    def test_a_batch_item_with_disagreeing_values_fails_alone(self):
        parsed = _request_from_item(
            {
                **REMEMBER_ARGS,
                "details": {"observed_at": "2024-03-08"},
                "observed_at": "2024-03-07",
            },
            _CTX,
        )
        assert isinstance(parsed, dict)
        assert parsed["error"] == "validation_error"


class TestHandlers:
    @pytest.mark.asyncio
    async def test_remember_with_disagreeing_values_is_an_invalid_argument(self):
        """The schema refuses it at dispatch, like any other bad argument (#1323)."""
        from mcp_server.tools import execute_tool_call

        with patch(
            "mcp_server.tools._check_rate_limit",
            new_callable=AsyncMock,
            return_value=(True, 0, 100),
        ):
            result = await execute_tool_call(
                "remember",
                {
                    "context_id": str(uuid4()),
                    **REMEMBER_ARGS,
                    "details": {"observed_at": "2024-03-08"},
                    "observed_at": "2024-03-07",
                },
                "test_user",
                uuid4(),
            )
        payload = _payload(result)
        assert payload["error"] == "invalid_argument"
        assert "disagree" in payload["message"]

    @pytest.mark.asyncio
    async def test_remember_echoes_the_services_refusal(self):
        from mcp_server.tools.memory import handle_remember

        service = MagicMock()
        service.remember = AsyncMock(
            side_effect=ValueError("invalid details.observed_at: observed_at must be ...")
        )
        result = await _run(
            handle_remember,
            {"context_id": str(uuid4()), **REMEMBER_ARGS, "observed_at": "soon"},
            _mock_db(),
            service,
        )
        payload = _payload(result)
        assert payload["error"] == "validation_error"
        assert payload["message"].startswith("invalid details.observed_at")

    @pytest.mark.asyncio
    async def test_update_memory_passes_it_to_the_service(self):
        from mcp_server.tools.memory import handle_update_memory

        service = MagicMock()
        service.update_memory = AsyncMock(side_effect=ValueError("stop here"))
        await _run(
            handle_update_memory,
            {"context_id": str(uuid4()), "memory_id": str(uuid4()), "observed_at": "2024-03-07"},
            _mock_db(),
            service,
        )
        request = service.update_memory.await_args.args[0]
        assert request.observed_at == "2024-03-07"
        assert request.details is None  # folded by the service once the row is loaded
