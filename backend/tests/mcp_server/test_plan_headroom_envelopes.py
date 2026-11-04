"""``plan_headroom`` on the write envelopes and on bootstrap (#2615).

The handlers run with the database and the services stubbed, inside the
collector ``execute_tool_call`` opens per call. A stubbed service records the
readings the real quota checks would (``record_quota_reading``), so the
handler's read of the collector, the throttle and the envelope are what is
under test. Redis is patched where ``plan_headroom`` imports it
(``set_if_absent``).
"""

from __future__ import annotations

import contextlib
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import mcp_server.tools as tools_mod
import mcp_server.tools._capacity_gate as capacity_gate
from mcp_server.tools import execute_tool_call, get_tool_definitions
from mcp_server.tools.batch import handle_remember_batch
from mcp_server.tools.memory import handle_remember, handle_update_memory
from services import plan_headroom as ph
from services.plan_headroom import (
    MCP_CALLS_PER_DAY,
    MEMORIES,
    MEMORIES_PER_DAY,
    QuotaReading,
    collect_plan_headroom_counts,
    current_plan_headroom_counts,
    record_quota_reading,
)
from utils.exceptions import RedisError
from utils.response_budget import json_chars

WS = uuid4()
RESETS_AT = "2026-11-05T00:00:00Z"


def _context():
    return SimpleNamespace(
        id=uuid4(),
        name="ctx",
        workspace_id=WS,
        is_private=False,
        last_used_at=None,
    )


def _record(memories: int, per_day: int = 0, *, limit: int = 10) -> None:
    """What check_memory_quota / check_memories_per_day record on a write."""
    record_quota_reading(MEMORIES, QuotaReading(WS, memories, limit, "free"))
    record_quota_reading(MEMORIES_PER_DAY, QuotaReading(WS, per_day, limit, "free", RESETS_AT))


def _remember_result():
    return SimpleNamespace(
        memory_id=uuid4(), scope="personal", version=1, persistence=None, lint=[]
    )


def _update_result():
    return SimpleNamespace(
        memory_id=uuid4(),
        operation="update",
        re_embedded=False,
        scope="personal",
        version=2,
        persistence=None,
        supersede_candidate_dismissed=None,
        superseded_memory_id=None,
        lint=[],
    )


@pytest.fixture
def first_this_hour():
    with patch.object(ph, "set_if_absent", new=AsyncMock(return_value=True)) as m:
        yield m


@contextlib.contextmanager
def _handlers(service, *, module="mcp_server.tools.memory", db=None):
    db = db if db is not None else AsyncMock()

    async def mock_get_db():
        yield db

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch(f"{module}._resolve_context", new=AsyncMock(return_value=_context())),
        patch(f"{module}._context_response_fields", return_value={}),
        patch(f"{module}._log_tool_usage", new=AsyncMock()),
        patch(f"{module}._touch_context_last_used", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        collect_plan_headroom_counts(),  # what execute_tool_call opens per call
    ):
        yield db


def _remember_service(memories: int, per_day: int = 0) -> MagicMock:
    async def remember(*_a, **_k):
        _record(memories, per_day)
        return _remember_result()

    service = MagicMock()
    service.remember = AsyncMock(side_effect=remember)
    return service


async def _remember(service) -> dict:
    with _handlers(service):
        (block,) = await handle_remember(
            {
                "summary": "a summary long enough for the lint",
                "content": "c",
                "type": "note",
                "context_id": str(uuid4()),
            },
            user_id="u1",
            workspace_id=None,
        )
    return json.loads(block.text)


# ------------------------------------------------------------------ remember


@pytest.mark.asyncio
async def test_remember_at_80_percent_carries_the_notice(first_this_hour):
    payload = await _remember(_remember_service(8))
    assert payload["status"] == "success"
    (notice,) = payload["plan_headroom"]
    assert notice["dimension"] == "memories"
    assert (notice["level"], notice["used"], notice["limit"]) == ("warning", 8, 10)
    assert notice["remedies"] == ["cleanup", "upgrade"]
    first_this_hour.assert_awaited_once()


@pytest.mark.asyncio
async def test_remember_at_95_percent_is_critical_and_names_the_daily_cap(first_this_hour):
    payload = await _remember(_remember_service(10, per_day=9))
    assert [(n["dimension"], n["level"]) for n in payload["plan_headroom"]] == [
        ("memories", "critical"),
        ("memories_per_day", "warning"),
    ]
    assert payload["plan_headroom"][1]["resets_at"] == RESETS_AT


@pytest.mark.asyncio
async def test_remember_below_80_percent_has_no_field(first_this_hour):
    payload = await _remember(_remember_service(7))
    assert payload["status"] == "success"
    assert "plan_headroom" not in payload
    first_this_hour.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_second_write_within_the_hour_is_quiet(first_this_hour):
    first_this_hour.return_value = False
    payload = await _remember(_remember_service(8))
    assert payload["status"] == "success"
    assert "plan_headroom" not in payload


@pytest.mark.asyncio
async def test_a_redis_failure_shows_the_notice(first_this_hour):
    first_this_hour.side_effect = RedisError("redis down")
    payload = await _remember(_remember_service(8))
    assert [n["level"] for n in payload["plan_headroom"]] == ["warning"]


@pytest.mark.asyncio
async def test_a_failing_read_never_fails_the_write(first_this_hour):
    """Nothing recorded and the workspace read raises: the write still succeeds."""
    service = MagicMock()
    service.remember = AsyncMock(return_value=_remember_result())
    db = AsyncMock()
    db.get = AsyncMock(side_effect=RuntimeError("db gone"))
    with _handlers(service, db=db):
        (block,) = await handle_remember(
            {
                "summary": "a summary long enough",
                "content": "c",
                "type": "note",
                "context_id": str(uuid4()),
            },
            user_id="u1",
            workspace_id=None,
        )
    payload = json.loads(block.text)
    assert payload["status"] == "success"
    assert "plan_headroom" not in payload


# ------------------------------------------------------------- update_memory


@pytest.mark.asyncio
async def test_update_memory_carries_the_notice(first_this_hour):
    """The external_id create half goes through remember's checks."""

    async def update(*_a, **_k):
        _record(10)
        return _update_result()

    service = MagicMock()
    service.update_memory = AsyncMock(side_effect=update)
    with _handlers(service):
        (block,) = await handle_update_memory(
            {"memory_id": str(uuid4()), "summary": "edited summary", "context_id": str(uuid4())},
            user_id="u1",
            workspace_id=None,
        )
    payload = json.loads(block.text)
    assert payload["status"] == "success"
    assert [(n["dimension"], n["level"]) for n in payload["plan_headroom"]] == [
        ("memories", "critical")
    ]


@pytest.mark.asyncio
async def test_an_in_place_update_reads_the_counts_once(first_this_hour):
    """No quota check ran: the numbers are read the way get_usage reads them."""
    service = MagicMock()
    service.update_memory = AsyncMock(return_value=_update_result())
    db = AsyncMock()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            plan_name="basic",
            effective_memory_limit=10,
            effective_memories_per_day=50,
            effective_mcp_calls_per_day=1000,
        )
    )
    live = AsyncMock(return_value=9)
    today = AsyncMock(return_value=3)
    with (
        _handlers(service, db=db),
        patch.object(ph, "count_live_memories", new=live),
        patch("services.quota_service.QuotaService.count_memories_created_today", new=today),
    ):
        (block,) = await handle_update_memory(
            {"memory_id": str(uuid4()), "summary": "edited summary", "context_id": str(uuid4())},
            user_id="u1",
            workspace_id=None,
        )
    payload = json.loads(block.text)
    (notice,) = payload["plan_headroom"]
    assert (notice["dimension"], notice["used"], notice["plan"]) == ("memories", 9, "basic")
    db.get.assert_awaited_once()
    live.assert_awaited_once()
    today.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_external_id_replacement_counts_again(first_this_hour):
    """remember's checks recorded count + 1, then the replaced row was deleted."""

    async def update(*_a, **_k):
        _record(9)
        return SimpleNamespace(**{**vars(_update_result()), "operation": "replaced"})

    service = MagicMock()
    service.update_memory = AsyncMock(side_effect=update)
    db = AsyncMock()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            plan_name="free",
            effective_memory_limit=10,
            effective_memories_per_day=50,
            effective_mcp_calls_per_day=1000,
        )
    )
    live = AsyncMock(return_value=8)
    with _handlers(service, db=db), patch.object(ph, "count_live_memories", new=live):
        (block,) = await handle_update_memory(
            {
                "external_id": "doc-1",
                "summary": "replaced summary",
                "content": "c",
                "type": "note",
                "context_id": str(uuid4()),
            },
            user_id="u1",
            workspace_id=None,
        )
    payload = json.loads(block.text)
    assert "plan_headroom" in payload, payload
    (notice,) = payload["plan_headroom"]
    assert (notice["used"], notice["level"]) == (8, "warning")
    live.assert_awaited_once()


# ------------------------------------------------------------ remember_batch


def _batch_args(n: int = 2, **extra) -> dict:
    items = [
        {"summary": f"batch item number {i} summary", "content": "c", "type": "note"}
        for i in range(n)
    ]
    return {"context_id": str(uuid4()), "items": items, **extra}


@pytest.mark.asyncio
async def test_remember_batch_carries_one_notice_set_for_the_call(first_this_hour):
    """Each item's checks record; the batch reports the last reading once."""
    used = iter([8, 9])

    async def remember(*_a, **_k):
        _record(next(used))
        return _remember_result()

    service = MagicMock()
    service.remember = AsyncMock(side_effect=remember)
    with _handlers(service, module="mcp_server.tools.batch"):
        (block,) = await handle_remember_batch(_batch_args(2), "u1", None)
    payload = json.loads(block.text)
    assert payload["status"] == "success"
    (notice,) = payload["plan_headroom"]
    assert (notice["used"], notice["level"]) == (9, "warning")
    assert all("plan_headroom" not in item for item in payload["results"])
    first_this_hour.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_atomic_batch_carries_the_notice(first_this_hour):
    async def remember_many(requests, **_k):
        _record(10, per_day=len(requests))
        return [_remember_result() for _ in requests]

    service = MagicMock()
    service.remember_many = AsyncMock(side_effect=remember_many)
    with _handlers(service, module="mcp_server.tools.batch"):
        (block,) = await handle_remember_batch(_batch_args(3, atomic=True), "u1", None)
    payload = json.loads(block.text)
    assert payload["status"] == "success"
    assert [n["level"] for n in payload["plan_headroom"]] == ["critical"]


@pytest.mark.asyncio
async def test_a_batch_that_wrote_nothing_has_no_notice(first_this_hour):
    from utils.exceptions import QuotaExceededError

    async def remember(*_a, **_k):
        _record(10)
        raise QuotaExceededError("Memory quota exceeded.")

    service = MagicMock()
    service.remember = AsyncMock(side_effect=remember)
    with _handlers(service, module="mcp_server.tools.batch"):
        (block,) = await handle_remember_batch(_batch_args(2), "u1", None)
    payload = json.loads(block.text)
    assert payload["error"] == "batch_failed"
    assert "plan_headroom" not in payload
    first_this_hour.assert_not_awaited()


# ----------------------------------------------------------------- bootstrap


async def _bootstrap(args, *, used=8):
    from mcp_server.tools import bootstrap as module

    db = AsyncMock()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            plan_name="free",
            effective_memory_limit=10,
            effective_memories_per_day=50,
            effective_mcp_calls_per_day=1000,
        )
    )

    async def gen():
        yield db

    service = MagicMock()
    service.build = AsyncMock(return_value={"status": "success"})
    with ExitStack() as stack:
        stack.enter_context(patch("db.base.get_db", new=gen))
        stack.enter_context(
            patch.object(
                module, "_resolve_context_for_read", new=AsyncMock(return_value=_context())
            )
        )
        stack.enter_context(patch.object(module, "_log_tool_usage", new=AsyncMock()))
        stack.enter_context(patch.object(module, "_context_response_fields", return_value={}))
        stack.enter_context(
            patch(
                "services.session_bootstrap_service.SessionBootstrapService", return_value=service
            )
        )
        stack.enter_context(
            patch.object(ph, "count_live_memories", new=AsyncMock(return_value=used))
        )
        stack.enter_context(
            patch(
                "services.quota_service.QuotaService.count_memories_created_today",
                new=AsyncMock(return_value=0),
            )
        )
        stack.enter_context(
            patch(
                "services.quota_service.QuotaService.count_mcp_calls_today",
                new=AsyncMock(return_value=0),
            )
        )
        stack.enter_context(collect_plan_headroom_counts())
        (block,) = await module.handle_bootstrap({"context_id": str(uuid4()), **args}, "user-1", WS)
    return service.build.await_args.kwargs, json.loads(block.text)


@pytest.mark.asyncio
async def test_bootstrap_always_carries_the_notice(first_this_hour):
    """Never throttled: the key is neither read nor armed at a session start."""
    first_this_hour.return_value = False  # a write already showed it this hour
    _, reply = await _bootstrap({})
    (notice,) = reply["plan_headroom"]
    assert (notice["dimension"], notice["level"]) == ("memories", "warning")
    first_this_hour.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_holds_max_chars_with_the_notice(first_this_hour):
    kwargs, reply = await _bootstrap({"max_chars": 3_000})
    field = {"plan_headroom": reply["plan_headroom"]}
    assert kwargs["format"] == "index"
    assert kwargs["max_chars"] == 3_000 - json_chars({}) - json_chars(field)


@pytest.mark.asyncio
async def test_bootstrap_below_80_percent_keeps_its_budget(first_this_hour):
    kwargs, reply = await _bootstrap({}, used=7)
    assert "plan_headroom" not in reply
    assert kwargs["max_chars"] == 20_000


# ---------------------------------------------------------------- dispatcher


@pytest.fixture
def dispatched(monkeypatch):
    """The registry's stubs record the MCP-call reading their handler sees."""
    seen: dict[str, object] = {}

    def stub(name):
        async def handler(_args, _user_id, _workspace_id):
            counts = current_plan_headroom_counts()
            seen[name] = counts.readings.get(MCP_CALLS_PER_DAY) if counts else "no collector"
            return [tools_mod.TextContent(type="text", text='{"status":"success"}')]

        return handler

    async def no_lock(_tool_name, _args, _workspace_id):
        return None

    monkeypatch.setattr(capacity_gate, "capacity_lock_refusal", no_lock)
    monkeypatch.setattr(
        tools_mod, "_TOOL_REGISTRY", {d["name"]: stub(d["name"]) for d in get_tool_definitions()}
    )
    with patch.object(tools_mod, "_check_rate_limit", new=AsyncMock(return_value=(True, 79, 100))):
        yield seen


@pytest.mark.asyncio
async def test_the_dispatcher_hands_its_mcp_count_to_the_handler(dispatched):
    args = {
        "summary": "a summary long enough",
        "content": "c",
        "type": "note",
        "context_id": str(uuid4()),
    }
    await execute_tool_call("remember", args, "user-1", WS)
    reading = dispatched["remember"]
    assert isinstance(reading, QuotaReading)
    assert (reading.workspace_id, reading.used, reading.limit) == (WS, 80, 100)
    assert reading.resets_at and reading.resets_at.endswith("T00:00:00Z")
    assert current_plan_headroom_counts() is None  # closed after the call


@pytest.mark.asyncio
async def test_an_exempt_tool_gets_no_mcp_count(dispatched):
    await execute_tool_call("bootstrap", {"context_id": str(uuid4())}, "user-1", WS)
    assert dispatched["bootstrap"] is None


# --------------------------------------------------------------------- docs


@pytest.mark.parametrize("name", ["remember", "remember_batch", "update_memory", "bootstrap"])
def test_the_four_tools_name_the_field_in_their_returns_line(name):
    tool = next(t for t in get_tool_definitions() if t["name"] == name)
    returns = next(line for line in tool["description"].splitlines() if line.startswith("Returns:"))
    assert "plan_headroom?" in returns


def test_the_guide_explains_the_field_next_to_quota_exceeded():
    from mcp_server.tools.guide import GUIDE_TOPICS

    text = GUIDE_TOPICS["remember.returns"]
    assert "plan_headroom" in text
    assert text.index("plan_headroom") < text.index("quota_exceeded") + 400
