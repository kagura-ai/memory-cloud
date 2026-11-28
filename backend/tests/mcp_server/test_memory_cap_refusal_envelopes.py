"""The total memory cap refusal on every surface that answers it (#3022).

MCP ``remember``, ``remember_batch`` (atomic and not), the create half of
``update_memory(external_id=...)``, the google tool profile and REST
``POST /memory/remember``: the typed details, ``remedies``, the caller's
``never_delivered_count`` (read once per call, after the rollback), the count
sentence in the message and the ``help``. The count itself is stubbed here;
the DB-backed half is ``tests/integration/test_3022_plan_limit_refusal_db.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.plan_tiers import PlanName, quota_gate_details
from services import memory_cap_refusal as mcr
from services.plan_headroom import memory_cap_help, never_delivered_sentence
from utils.exceptions import QuotaExceededError

MESSAGE = "Memory quota exceeded. Current: 5, Limit: 5 (free plan + addons)"
REMEDIES = ["cleanup", "upgrade"]


def _cap(*, target: bool = True) -> QuotaExceededError:
    exc = QuotaExceededError(
        MESSAGE,
        **quota_gate_details(
            PlanName.FREE, "memories", current=5, limit=5, required_plan=PlanName.BASIC
        ),
        remedies=list(REMEDIES),
    )
    if target:
        mcr.set_memory_cap_target(exc, mcr.MemoryCapTarget(context_id=uuid4(), is_private=False))
    return exc


def _daily() -> QuotaExceededError:
    return QuotaExceededError(
        "Daily memory-creation quota exceeded.",
        **quota_gate_details(
            PlanName.FREE, "memories_per_day", current=5, limit=5, resets_at="2026-01-01T00:00:00Z"
        ),
        used_today=5,
        requested=1,
    )


def _payload(result) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


def _db() -> MagicMock:
    return MagicMock(
        commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock(), execute=AsyncMock()
    )


def _counting(value):
    """``count_never_delivered`` answering ``value`` (or raising it)."""
    if isinstance(value, BaseException):
        return AsyncMock(side_effect=value)
    return AsyncMock(return_value=value)


# ------------------------------------------------------------------ remember


async def _remember(exc: BaseException, count) -> tuple[dict, AsyncMock, AsyncMock]:
    from mcp_server.tools.memory import handle_remember

    db = _db()

    async def get_db():
        yield db

    log_usage = AsyncMock()
    counter = _counting(count)
    service = MagicMock(remember=AsyncMock(side_effect=exc))
    with (
        patch("db.base.get_db", new=get_db),
        patch(
            "mcp_server.tools.memory._resolve_context",
            new=AsyncMock(return_value=MagicMock(id=uuid4())),
        ),
        patch("mcp_server.tools.memory._log_tool_usage", new=log_usage),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        patch.object(mcr, "count_never_delivered", new=counter),
    ):
        result = await handle_remember(
            {
                "context_id": str(uuid4()),
                "summary": "a summary long enough",
                "content": "c",
                "type": "note",
            },
            "alice",
            uuid4(),
        )
    return _payload(result), log_usage, counter


class TestRemember:
    @pytest.mark.asyncio
    async def test_a_count_above_zero_is_named_with_the_remedies(self) -> None:
        payload, log_usage, counter = await _remember(_cap(), 3)

        assert payload["status"] == "error"
        assert payload["error"] == "quota_exceeded"
        assert payload["gate"] == "quota"
        assert payload["quota_type"] == "memories"
        assert (payload["current"], payload["limit"]) == (5, 5)
        assert payload["required_plan"] == PlanName.BASIC
        assert payload["remedies"] == REMEDIES
        assert payload["never_delivered_count"] == 3
        assert payload["message"] == f"{MESSAGE}. {never_delivered_sentence(3)}"
        assert payload["help"] == memory_cap_help(3, REMEDIES)
        assert log_usage.await_args.args[4] == 429
        counter.assert_awaited_once()
        assert counter.await_args.kwargs["viewer_user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_a_count_of_zero_is_sent_without_the_sentence(self) -> None:
        payload, _log, _counter = await _remember(_cap(), 0)

        assert payload["never_delivered_count"] == 0
        assert payload["message"] == MESSAGE
        assert payload["help"] == memory_cap_help(0, REMEDIES)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [RuntimeError("db gone"), None])
    async def test_an_unread_count_leaves_the_field_out(self, count) -> None:
        exc = _cap() if count is not None else _cap(target=False)
        payload, log_usage, _counter = await _remember(exc, count if count is not None else 9)

        assert payload["error"] == "quota_exceeded"
        assert payload["quota_type"] == "memories"
        assert "never_delivered_count" not in payload
        assert payload["message"] == MESSAGE
        assert payload["remedies"] == REMEDIES
        assert payload["help"] == memory_cap_help(None, REMEDIES)
        assert log_usage.await_args.args[4] == 429

    @pytest.mark.asyncio
    async def test_the_daily_cap_reads_no_count_and_keeps_its_envelope(self) -> None:
        payload, _log, counter = await _remember(_daily(), 3)

        counter.assert_not_awaited()
        assert payload["quota_type"] == "memories_per_day"
        for key in ("never_delivered_count", "remedies", "help"):
            assert key not in payload
        assert payload["message"] == "Daily memory-creation quota exceeded."


# ------------------------------------------------------------- update_memory


async def _update(exc: BaseException, count) -> tuple[dict, AsyncMock, AsyncMock]:
    from mcp_server.tools.memory import handle_update_memory

    db = _db()

    async def get_db():
        yield db

    log_usage = AsyncMock()
    counter = _counting(count)
    service = MagicMock(update_memory=AsyncMock(side_effect=exc))
    with (
        patch("db.base.get_db", new=get_db),
        patch(
            "mcp_server.tools.memory._resolve_context",
            new=AsyncMock(return_value=MagicMock(id=uuid4())),
        ),
        patch("mcp_server.tools.memory._log_tool_usage", new=log_usage),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        patch.object(mcr, "count_never_delivered", new=counter),
    ):
        result = await handle_update_memory(
            {
                "context_id": str(uuid4()),
                "external_id": "sync-1",
                "summary": "a summary long enough",
                "content": "c",
                "type": "note",
            },
            "alice",
            uuid4(),
        )
    return _payload(result), log_usage, counter


class TestUpdateMemoryCreateHalf:
    @pytest.mark.asyncio
    async def test_the_total_cap_answers_like_remember_and_logs_429(self) -> None:
        payload, log_usage, counter = await _update(_cap(), 2)

        assert payload["error"] == "quota_exceeded"
        assert payload["quota_type"] == "memories"
        assert payload["never_delivered_count"] == 2
        assert payload["message"] == f"{MESSAGE}. {never_delivered_sentence(2)}"
        assert payload["help"] == memory_cap_help(2, REMEDIES)
        assert log_usage.await_args.args[4] == 429
        counter.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_daily_cap_keeps_the_dispatcher_envelope_and_logs_429(self) -> None:
        from mcp_server.tools._errors import _tool_exception_response

        exc = _daily()
        payload, log_usage, counter = await _update(exc, 2)

        counter.assert_not_awaited()
        assert payload == _payload(_tool_exception_response("update_memory", exc))
        assert log_usage.await_args.args[4] == 429

    @pytest.mark.asyncio
    async def test_the_size_guard_is_logged_as_413(self) -> None:
        from utils.exceptions import ContentSizeExceededError

        exc = ContentSizeExceededError(limit=10, actual=11)
        payload, log_usage, _counter = await _update(exc, 2)

        assert payload["error"] == "payload_too_large"
        assert log_usage.await_args.args[4] == 413


# ------------------------------------------------------------ remember_batch


def _response(*_a, **_k):
    return SimpleNamespace(memory_id=uuid4(), scope="working", persistence=None, lint=[])


async def _batch(service: MagicMock, *, atomic: bool, count) -> tuple[dict, AsyncMock]:
    from mcp_server.tools.batch import handle_remember_batch

    db = _db()

    async def get_db():
        yield db

    counter = _counting(count)
    items = [
        {"summary": f"item number {i} summary", "content": "c", "type": "note"} for i in range(3)
    ]
    with (
        patch("db.base.get_db", new=get_db),
        patch("services.memory_service.MemoryService", return_value=service),
        patch(
            "mcp_server.tools.batch._resolve_context",
            new=AsyncMock(return_value=MagicMock(workspace_id=uuid4())),
        ),
        patch("mcp_server.tools.batch._context_response_fields", return_value={}),
        patch("mcp_server.tools.batch._log_tool_usage", new=AsyncMock()),
        patch("mcp_server.tools.batch._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.batch._plan_headroom_field", new=AsyncMock(return_value={})),
        patch.object(mcr, "count_never_delivered", new=counter),
    ):
        result = await handle_remember_batch(
            {"context_id": str(uuid4()), "items": items, "atomic": atomic}, "alice", None
        )
    return _payload(result), counter


class TestRememberBatch:
    @pytest.mark.asyncio
    async def test_non_atomic_reads_the_count_once_and_keeps_the_items(self) -> None:
        # The first item fits, the next two are refused by the total cap.
        service = MagicMock(
            tag_canonical_map=AsyncMock(return_value={}),
            remember=AsyncMock(side_effect=[_response(), _cap(), _cap()]),
        )
        payload, counter = await _batch(service, atomic=False, count=4)

        counter.assert_awaited_once()
        assert payload["status"] == "partial"
        assert payload["never_delivered_count"] == 4
        assert payload["remedies"] == REMEDIES
        assert payload["help"] == memory_cap_help(4, REMEDIES)
        refused = [r for r in payload["results"] if r["status"] == "error"]
        assert len(refused) == 2
        for item in refused:
            # The per-item shape is unchanged.
            assert set(item) == {"index", "status", "error", "message"}
            assert item["error"] == "quota_exceeded"

    @pytest.mark.asyncio
    async def test_atomic_reads_the_count_for_the_refused_item(self) -> None:
        from services.memory_service import BatchItemError

        service = MagicMock(remember_many=AsyncMock(side_effect=BatchItemError(1, _cap())))
        payload, counter = await _batch(service, atomic=True, count=0)

        counter.assert_awaited_once()
        assert payload["error"] == "batch_failed"
        assert payload["never_delivered_count"] == 0
        assert payload["help"] == memory_cap_help(0, REMEDIES)
        assert [r["status"] for r in payload["results"]] == ["skipped", "error", "skipped"]

    @pytest.mark.asyncio
    async def test_a_batch_without_a_cap_refusal_reads_no_count(self) -> None:
        service = MagicMock(
            tag_canonical_map=AsyncMock(return_value={}),
            remember=AsyncMock(side_effect=[_response(), _daily(), _response()]),
        )
        payload, counter = await _batch(service, atomic=False, count=4)

        counter.assert_not_awaited()
        for key in ("never_delivered_count", "remedies", "help"):
            assert key not in payload


# ------------------------------------------------------------ google profile


class TestGoogleProfile:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [3, 0])
    async def test_the_refusal_names_no_tool_and_keeps_the_count(self, count) -> None:
        from mcp_server.tools._google_profile import _pointer_pattern, scrub_google_reply
        from mcp_server.tools._helpers import ToolErrorContent
        from mcp_server.tools.memory import memory_cap_response

        exc = _cap()
        with patch.object(mcr, "count_never_delivered", new=_counting(count)):
            reply = await memory_cap_response(_db(), exc, user_id="alice")
        scrubbed = scrub_google_reply("remember", reply)

        assert isinstance(scrubbed, ToolErrorContent)
        payload = _payload(scrubbed)
        pattern = _pointer_pattern("remember")
        assert not pattern.search(payload["help"])
        assert payload["help"] == memory_cap_help(count, REMEDIES, neutral=True)
        assert payload["never_delivered_count"] == count
        if count:
            assert payload["help"].startswith(never_delivered_sentence(count))
            assert payload["message"].endswith(never_delivered_sentence(count))


# ---------------------------------------------------------------------- REST


async def _rest(exc: BaseException, count):
    from api.main import memory_cloud_exception_handler
    from api.routes.memory import remember
    from models.schemas import RememberRequest

    db = _db()
    service = MagicMock(db=db, remember=AsyncMock(side_effect=exc))
    counter = _counting(count)
    with (
        patch.object(mcr, "count_never_delivered", new=counter),
        pytest.raises(QuotaExceededError) as raised,
    ):
        await remember(
            RememberRequest(
                summary="a summary long enough",
                content="c",
                type="note",
                context={"context_id": str(uuid4())},
            ),
            {"user_id": "alice"},
            service,
        )
    request = MagicMock()
    request.url.path = "/api/v1/memory/remember"
    response = await memory_cloud_exception_handler(request, raised.value)
    return response.status_code, json.loads(response.body), db, counter


class TestRest:
    @pytest.mark.asyncio
    async def test_the_429_carries_the_count_and_the_remedies(self) -> None:
        status, body, db, counter = await _rest(_cap(), 5)

        assert status == 429
        assert body["error"] == "QUOTA-001"
        assert body["message"] == f"{MESSAGE}. {never_delivered_sentence(5)}"
        details = body["details"]
        assert details["gate"] == "quota"
        assert details["quota_type"] == "memories"
        assert details["never_delivered_count"] == 5
        assert details["remedies"] == REMEDIES
        assert "help" not in details
        db.rollback.assert_awaited()
        assert counter.await_args.kwargs["viewer_user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_the_daily_cap_is_re_raised_untouched(self) -> None:
        status, body, db, counter = await _rest(_daily(), 5)

        assert status == 429
        counter.assert_not_awaited()
        db.rollback.assert_not_awaited()
        assert "never_delivered_count" not in body["details"]
        assert body["message"] == "Daily memory-creation quota exceeded."
