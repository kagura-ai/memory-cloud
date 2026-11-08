"""The MCP write tools echo the resolved event dates (``details.event_phrase``).

``remember``, each ``remember_batch`` item and ``update_memory`` carry
``event_start`` / ``event_end`` / ``event_weekday`` after ``observed_weekday``
when the stored memory has them, ``event_unresolved`` (``unknown_phrase`` or
``no_observed_at``) when it carries a phrase without dates, and nothing
otherwise. The write-tool descriptions carry the rule itself: pass the phrase,
do not compute dates.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from mcp_server.tools._definitions import get_tool_definitions
from models.schemas import RememberResponse, UpdateMemoryResponse
from tests.mcp_server.test_observed_at_tools import (
    _batch_payload,
    _remember_payload,
    _update_payload,
)
from tests.mcp_server.test_value_error_arms import REMEMBER_ARGS, _mock_db, _payload, _run


def _definition(name: str) -> dict:
    return next(tool for tool in get_tool_definitions() if tool["name"] == name)


def _remembered(**event) -> RememberResponse:
    return RememberResponse(memory_id=uuid4(), scope="working", **event)


def _updated(**event) -> UpdateMemoryResponse:
    return UpdateMemoryResponse(
        memory_id=uuid4(),
        operation="updated",
        re_embedded=False,
        scope="working",
        version=2,
        **event,
    )


RESOLVED = {
    "observed_at": "2024-03-11",
    "event_start": "2024-03-09",
    "event_end": "2024-03-10",
    "event_phrase": "last weekend",
}
UNKNOWN = {"observed_at": "2024-03-11", "event_phrase": "a few days ago"}
NO_ANCHOR = {"event_phrase": "yesterday"}


class TestDefinitions:
    @pytest.mark.parametrize("name", ["remember", "remember_batch", "update_memory"])
    def test_the_write_tools_say_pass_the_phrase_and_do_not_compute(self, name):
        description = _definition(name)["description"]
        assert "details.event_phrase" in description
        assert "do not compute dates" in description

    def test_remember_names_the_material_date_as_observed_at(self):
        description = _definition("remember")["description"]
        assert "observed_at is always the material's date" in description
        assert "never the write time" in description
        assert "event_start/event_end" in description and "event_unresolved" in description

    def test_the_details_parameter_lists_the_event_keys(self):
        details = _definition("remember")["inputSchema"]["properties"]["details"]["description"]
        for key in ("event_phrase", "event_start", "event_end", "observed_at"):
            assert key in details
        observed_at = _definition("remember")["inputSchema"]["properties"]["observed_at"]
        assert "never the write time" in observed_at["description"]
        assert "event_phrase" in observed_at["description"]
        resend = _definition("update_memory")["inputSchema"]["properties"]["details"]
        assert "event_phrase" in resend["description"]

    def test_the_guide_documents_the_phrase(self):
        from mcp_server.tools.guide import GUIDE_TOPICS

        topic = GUIDE_TOPICS["remember.observed_at"]
        assert "details.event_phrase" in topic and "do not compute dates" in topic
        assert "no_observed_at" in topic and "unknown_phrase" in topic
        for name in ("remember.returns", "update_memory.returns"):
            assert (
                "event_start?, event_end?, event_weekday?, event_unresolved?" in GUIDE_TOPICS[name]
            )


class TestRemember:
    @pytest.mark.asyncio
    async def test_the_dates_follow_the_weekday(self):
        payload = await _remember_payload(_remembered(**RESOLVED))
        assert (payload["event_start"], payload["event_end"]) == ("2024-03-09", "2024-03-10")
        assert payload["event_weekday"] == "Sat"
        keys = list(payload)
        assert keys.index("event_start") == keys.index("observed_weekday") + 1
        assert keys[keys.index("event_start") : keys.index("event_start") + 3] == [
            "event_start",
            "event_end",
            "event_weekday",
        ]
        assert "event_unresolved" not in payload

    @pytest.mark.asyncio
    async def test_an_unknown_phrase_is_flagged(self):
        payload = await _remember_payload(_remembered(**UNKNOWN))
        assert payload["event_unresolved"] == "unknown_phrase"
        assert "event_start" not in payload

    @pytest.mark.asyncio
    async def test_no_observed_at_is_flagged_with_its_reason(self):
        payload = await _remember_payload(_remembered(**NO_ANCHOR))
        assert payload["event_unresolved"] == "no_observed_at"
        assert "event_start" not in payload and "observed_at" not in payload

    @pytest.mark.asyncio
    async def test_without_a_phrase_the_response_is_unchanged(self):
        payload = await _remember_payload(_remembered())
        assert list(payload) == ["status", "memory_id", "scope", "version"]

    @pytest.mark.asyncio
    async def test_a_bad_phrase_is_echoed_as_a_validation_error(self):
        """What MemoryService._apply_event_dates raises (tests/services) reaches the caller."""
        from mcp_server.tools.memory import handle_remember

        service = MagicMock()
        service.remember = AsyncMock(
            side_effect=ValueError(
                "invalid details.event_phrase: event_phrase must be a non-empty string"
            )
        )
        result = await _run(
            handle_remember,
            {
                "context_id": str(uuid4()),
                **REMEMBER_ARGS,
                "details": {"observed_at": "2024-03-11", "event_phrase": ""},
            },
            _mock_db(),
            service,
        )
        payload = _payload(result)
        assert payload["error"] == "validation_error"
        assert payload["message"].startswith("invalid details.event_phrase")


class TestUpdateMemory:
    @pytest.mark.asyncio
    async def test_the_dates_follow_the_weekday(self):
        payload = await _update_payload(_updated(**RESOLVED))
        assert (payload["event_start"], payload["event_end"], payload["event_weekday"]) == (
            "2024-03-09",
            "2024-03-10",
            "Sat",
        )
        keys = list(payload)
        assert keys.index("event_start") == keys.index("observed_weekday") + 1

    @pytest.mark.asyncio
    async def test_unresolved_and_unchanged(self):
        assert (await _update_payload(_updated(**UNKNOWN)))["event_unresolved"] == "unknown_phrase"
        assert list(await _update_payload(_updated())) == [
            "status",
            "memory_id",
            "operation",
            "re_embedded",
            "scope",
            "version",
        ]


class TestRememberBatch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("atomic", [False, True])
    async def test_per_item(self, atomic):
        payload = await _batch_payload(
            [_remembered(**RESOLVED), _remembered(), _remembered(**NO_ANCHOR)], atomic=atomic
        )
        assert payload["status"] == "success"
        first, second, third = payload["results"]
        assert (first["event_start"], first["event_end"], first["event_weekday"]) == (
            "2024-03-09",
            "2024-03-10",
            "Sat",
        )
        keys = list(first)
        assert keys.index("event_start") == keys.index("observed_weekday") + 1
        assert list(second) == ["index", "status", "memory_id", "scope"]
        assert third["event_unresolved"] == "no_observed_at"
