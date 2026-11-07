"""``observed_at`` on remember / remember_batch / update_memory (#2330).

The top-level argument reaches the service folded into ``details``; a
conflicting or malformed value comes back as ``validation_error``; the
remember and update_memory definitions declare it. #2689: a write whose
stored memory has one echoes it with ``observed_weekday``.
"""

from __future__ import annotations

from contextlib import contextmanager
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

    @pytest.mark.parametrize("name", ["recall", "list"])
    def test_returns_name_the_weekday_next_to_it(self, name):
        """#2689: wherever a Returns line lists observed_at?, the weekday follows."""
        description = _definition(name)["description"]
        assert "observed_at?, observed_weekday?" in description

    def test_the_guide_names_the_weekday(self):
        from mcp_server.tools.guide import GUIDE_TOPICS

        assert "observed_weekday" in GUIDE_TOPICS["remember.observed_at"]
        for topic in ("recall.returns", "reference.returns"):
            assert "observed_at?, observed_weekday?" in GUIDE_TOPICS[topic]
        for topic in ("remember.returns", "update_memory.returns"):
            assert "observed_at?, observed_weekday?" in GUIDE_TOPICS[topic]

    def test_the_guide_check_is_for_a_date_only_value(self):
        """A datetime's weekday is its stored UTC day's, which can be the day
        before the material's local day (2024-03-11T08:00:00+09:00 is stored as
        2024-03-10T23:00:00Z, a Sunday); the fix-the-row check must not fire on it."""
        from mcp_server.tools.guide import GUIDE_TOPICS

        topic = GUIDE_TOPICS["remember.observed_at"]
        assert "a datetime's UTC day" in topic
        assert "from a date-only observed_at" in topic

    def test_the_bootstrap_description_names_both_exceptions(self):
        """get_agent_bootstrap's recall rows drop observed_weekday too (#2689)."""
        description = _definition("get_agent_bootstrap")["description"]
        assert "without supersede_candidate or observed_weekday" in description


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


# ---------------------------------------------------------------------------
# #2689: the write responses echo observed_at + observed_weekday
# ---------------------------------------------------------------------------


def _remembered(observed_at: str | None = None):
    from models.schemas import RememberResponse

    return RememberResponse(memory_id=uuid4(), scope="working", observed_at=observed_at)


def _updated(observed_at: str | None = None):
    from models.schemas import UpdateMemoryResponse

    return UpdateMemoryResponse(
        memory_id=uuid4(),
        operation="updated",
        re_embedded=False,
        scope="working",
        version=2,
        observed_at=observed_at,
    )


@contextmanager
def _write_handlers(service, module: str):
    db = _mock_db()

    async def get_db():
        yield db

    with (
        patch("db.base.get_db", new=get_db),
        patch(f"{module}._resolve_context", new=AsyncMock(return_value=MagicMock())),
        patch(f"{module}._context_response_fields", return_value={}),
        patch(f"{module}._log_tool_usage", new=AsyncMock()),
        patch(f"{module}._touch_context_last_used", new=AsyncMock()),
        patch(f"{module}._plan_headroom_field", new=AsyncMock(return_value={})),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        yield


async def _remember_payload(result) -> dict:
    from mcp_server.tools.memory import handle_remember

    service = MagicMock(remember=AsyncMock(return_value=result))
    with _write_handlers(service, "mcp_server.tools.memory"):
        reply = await handle_remember(
            {"context_id": _CTX, **REMEMBER_ARGS, "observed_at": "2024-03-11"}, "u1", None
        )
    return _payload(reply)


async def _update_payload(result) -> dict:
    from mcp_server.tools.memory import handle_update_memory

    service = MagicMock(update_memory=AsyncMock(return_value=result))
    with _write_handlers(service, "mcp_server.tools.memory"):
        reply = await handle_update_memory(
            {"context_id": _CTX, "memory_id": str(uuid4()), "summary": "an edited summary"},
            "u1",
            None,
        )
    return _payload(reply)


async def _batch_payload(results: list, *, atomic: bool) -> dict:
    from mcp_server.tools.batch import handle_remember_batch

    service = MagicMock(
        remember=AsyncMock(side_effect=results),
        remember_many=AsyncMock(return_value=results),
    )
    items = [{**REMEMBER_ARGS, "summary": f"batch item number {i}"} for i in range(len(results))]
    with _write_handlers(service, "mcp_server.tools.batch"):
        reply = await handle_remember_batch(
            {"context_id": _CTX, "items": items, "atomic": atomic}, "u1", None
        )
    return _payload(reply)


_OBSERVED = [("2024-03-11", "Mon"), ("2024-03-14T23:30:00Z", "Thu")]


class TestWriteResponsesEchoTheWeekday:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("observed_at", "weekday"), _OBSERVED)
    async def test_remember(self, observed_at, weekday):
        payload = await _remember_payload(_remembered(observed_at))
        assert payload["observed_at"] == observed_at
        assert payload["observed_weekday"] == weekday
        keys = list(payload)
        assert keys.index("observed_weekday") == keys.index("observed_at") + 1

    @pytest.mark.asyncio
    async def test_remember_without_it_is_unchanged(self):
        payload = await _remember_payload(_remembered())
        assert list(payload) == ["status", "memory_id", "scope", "version"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("observed_at", "weekday"), _OBSERVED)
    async def test_update_memory(self, observed_at, weekday):
        payload = await _update_payload(_updated(observed_at))
        assert payload["observed_at"] == observed_at
        assert payload["observed_weekday"] == weekday
        keys = list(payload)
        assert keys.index("observed_weekday") == keys.index("observed_at") + 1

    @pytest.mark.asyncio
    async def test_update_memory_without_it_is_unchanged(self):
        payload = await _update_payload(_updated())
        assert list(payload) == [
            "status",
            "memory_id",
            "operation",
            "re_embedded",
            "scope",
            "version",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("atomic", [False, True])
    async def test_remember_batch_per_item(self, atomic):
        payload = await _batch_payload(
            [_remembered("2024-03-11"), _remembered(), _remembered("2024-03-14T23:30:00Z")],
            atomic=atomic,
        )
        assert payload["status"] == "success"
        first, second, third = payload["results"]
        assert (first["observed_at"], first["observed_weekday"]) == ("2024-03-11", "Mon")
        keys = list(first)
        assert keys.index("observed_weekday") == keys.index("observed_at") + 1
        assert list(second) == ["index", "status", "memory_id", "scope"]
        assert third["observed_weekday"] == "Thu"
