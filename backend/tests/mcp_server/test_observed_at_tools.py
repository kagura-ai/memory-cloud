"""``observed_at`` on remember / remember_batch / update_memory (#2330).

The top-level argument reaches the service folded into ``details``; a
conflicting or malformed value comes back as ``validation_error``; the
remember and update_memory definitions declare it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
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
    def test_reaches_the_service_as_an_argument(self):
        request = remember_request_from_args(
            {**REMEMBER_ARGS, "details": {"k": 1}, "observed_at": "2024-03-07"}
        )
        # MemoryService folds it into details (the conflict check lives there).
        assert request.observed_at == "2024-03-07"
        assert request.details == {"k": 1}

    def test_a_batch_item_takes_it(self):
        parsed = _request_from_item({**REMEMBER_ARGS, "observed_at": "2024-03-07"}, _CTX)
        assert not isinstance(parsed, dict), parsed
        assert parsed.request.observed_at == "2024-03-07"

    def test_a_batch_items_conflict_is_a_validation_error(self):
        from mcp_server.tools.batch import item_error

        assert item_error(_CONFLICT)["error"] == "validation_error"


# What MemoryService raises for a disagreeing pair on every write path.
_CONFLICT = ValueError(
    "invalid details.observed_at: details.observed_at and observed_at disagree; give one of them"
)


class TestHandlers:
    @pytest.mark.asyncio
    async def test_remember_with_disagreeing_values_is_a_validation_error(self):
        """The same code as update_memory, PATCH and remember_batch (#2330 review):
        the real service refuses it before touching the database."""
        from mcp_server.tools.memory import handle_remember
        from services.memory_service import MemoryService

        result = await _run(
            handle_remember,
            {
                "context_id": str(uuid4()),
                **REMEMBER_ARGS,
                "details": {"observed_at": "2024-03-08"},
                "observed_at": "2024-03-07",
            },
            _mock_db(),
            MemoryService(MagicMock()),
        )
        payload = _payload(result)
        assert payload["error"] == "validation_error"
        assert "disagree" in payload["message"]

    @pytest.mark.asyncio
    async def test_update_memory_reports_the_conflict_the_same_way(self):
        from mcp_server.tools.memory import handle_update_memory

        service = MagicMock()
        service.update_memory = AsyncMock(side_effect=_CONFLICT)
        result = await _run(
            handle_update_memory,
            {
                "context_id": str(uuid4()),
                "memory_id": str(uuid4()),
                "details": {"observed_at": "2024-03-08"},
                "observed_at": "2024-03-07",
            },
            _mock_db(),
            service,
        )
        assert _payload(result)["error"] == "validation_error"

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
