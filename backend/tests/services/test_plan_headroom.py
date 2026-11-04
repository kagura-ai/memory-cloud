"""The plan headroom notice (#2615): thresholds, wire shape, throttle, readings.

``headroom_notices`` is pure over the numbers; ``plan_headroom`` completes the
readings a write did not record and applies the hourly throttle. Redis is
patched where ``plan_headroom`` imports it (``set_if_absent``), so no live
Redis is needed; the quota checks are driven with the mocked sessions
``test_quota_memories_per_day.py`` uses.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.plan_tiers import PLAN_ORDER, PLAN_TIERS
from services import plan_headroom as ph
from services.plan_headroom import (
    MCP_CALLS_PER_DAY,
    MEMORIES,
    MEMORIES_PER_DAY,
    PlanHeadroomCounts,
    QuotaReading,
    close_plan_headroom_counts,
    collect_plan_headroom_counts,
    current_plan_headroom_counts,
    drop_quota_reading,
    headroom_notices,
    mcp_calls_reading,
    open_plan_headroom_counts,
    plan_headroom,
    record_quota_reading,
)
from utils.exceptions import RedisError

WS = uuid4()
RESETS_AT = "2026-11-05T00:00:00Z"
TOP_PLAN = str(PLAN_ORDER[-1])
NOW = datetime(2026, 11, 4, 12, 0, 0)


def _reading(used: int, limit: int, *, plan: str = "free", resets_at: str | None = None):
    return QuotaReading(workspace_id=WS, used=used, limit=limit, plan=plan, resets_at=resets_at)


def _counts(**readings: QuotaReading) -> PlanHeadroomCounts:
    counts = PlanHeadroomCounts()
    counts.readings.update(readings)
    return counts


@pytest.fixture
def first_this_hour():
    """The throttle's SET NX: True = the key was free (notice shown)."""
    with patch.object(ph, "set_if_absent", new=AsyncMock(return_value=True)) as m:
        yield m


# ------------------------------------------------------------- thresholds


@pytest.mark.parametrize(
    ("used", "expected"),
    [
        (799, None),  # 79.9 %
        (800, "warning"),  # 80 %
        (949, "warning"),  # 94.9 %
        (950, "critical"),  # 95 %
        (1000, "critical"),  # 100 %
        (1003, "critical"),  # over (a downgraded workspace)
    ],
)
def test_thresholds(used, expected):
    notices = headroom_notices({MEMORIES: _reading(used, 1000)})
    if expected is None:
        assert notices == []
    else:
        (notice,) = notices
        assert notice["level"] == expected
        assert (notice["used"], notice["limit"]) == (used, 1000)


def test_percentage_is_rounded_to_one_decimal():
    (notice,) = headroom_notices({MEMORIES: _reading(949, 1000)})
    assert notice["percentage"] == 94.9
    (notice,) = headroom_notices({MEMORIES: _reading(5, 6)})
    assert notice["percentage"] == 83.3


@pytest.mark.parametrize(
    ("used", "level", "percentage"),
    [
        (9497, "warning", 94.9),
        (9500, "critical", 95.0),
        (7999, None, None),
        (8000, "warning", 80.0),
    ],
)
def test_percentage_never_reaches_a_threshold_the_level_has_not(used, level, percentage):
    """Rounded down: 94.97 % is a warning and reads 94.9, not 95.0."""
    notices = headroom_notices({MEMORIES: _reading(used, 10_000)})
    assert [(n["level"], n["percentage"]) for n in notices] == (
        [(level, percentage)] if level else []
    )


@pytest.mark.parametrize("dimension", [MEMORIES, MEMORIES_PER_DAY, MCP_CALLS_PER_DAY])
def test_a_zero_limit_never_produces_a_notice(dimension):
    """0 is a dimension the tier excludes (#569 zero floor), not one that is full."""
    assert headroom_notices({dimension: _reading(5, 0, resets_at=RESETS_AT)}) == []


# ---------------------------------------------------------------- wire shape


def test_entry_shape_for_the_total_cap():
    (notice,) = headroom_notices({MEMORIES: _reading(8, 10, plan="basic")})
    assert list(notice) == [
        "dimension",
        "level",
        "used",
        "limit",
        "percentage",
        "plan",
        "remedies",
        "help",
    ]
    assert notice["dimension"] == "memories"
    assert notice["plan"] == "basic"
    assert notice["remedies"] == ["cleanup", "upgrade"]
    assert "resets_at" not in notice


@pytest.mark.parametrize("dimension", [MEMORIES_PER_DAY, MCP_CALLS_PER_DAY])
def test_the_daily_dimensions_carry_resets_at(dimension):
    (notice,) = headroom_notices({dimension: _reading(8, 10, resets_at=RESETS_AT)})
    assert notice["resets_at"] == RESETS_AT
    assert list(notice).index("resets_at") == list(notice).index("percentage") + 1


@pytest.mark.parametrize("dimension", [MEMORIES_PER_DAY, MCP_CALLS_PER_DAY])
def test_deleting_memories_is_not_offered_for_a_daily_counter(dimension):
    """forget does not give back a day's creations or calls: the remedy is the reset."""
    (notice,) = headroom_notices({dimension: _reading(8, 10, resets_at=RESETS_AT)})
    assert notice["remedies"] == ["wait", "upgrade"]
    assert "forget" not in notice["help"]
    assert RESETS_AT in notice["help"]


def test_the_top_tier_offers_no_upgrade():
    readings = {
        MEMORIES: _reading(9, 10, plan=TOP_PLAN),
        MEMORIES_PER_DAY: _reading(9, 10, plan=TOP_PLAN, resets_at=RESETS_AT),
        MCP_CALLS_PER_DAY: _reading(9, 10, plan=TOP_PLAN, resets_at=RESETS_AT),
    }
    notices = headroom_notices(readings)
    assert [n["remedies"] for n in notices] == [["cleanup"], ["wait"], ["wait"]]
    assert all("upgrade" not in n["help"].lower() for n in notices)


def test_upgrade_is_offered_only_where_a_higher_tier_raises_that_limit():
    """The tier below the top shares the top's memory_limit: no upgrade for that cap."""
    below_top, top = PLAN_ORDER[-2], PLAN_ORDER[-1]
    tier = dataclasses.replace(
        PLAN_TIERS[below_top],
        memory_limit=PLAN_TIERS[top].memory_limit,
        memories_per_day=PLAN_TIERS[top].memories_per_day - 1,
    )
    readings = {
        MEMORIES: _reading(9, 10, plan=str(below_top)),
        MEMORIES_PER_DAY: _reading(9, 10, plan=str(below_top), resets_at=RESETS_AT),
    }
    with patch.dict(PLAN_TIERS, {below_top: tier}):
        memories, per_day = headroom_notices(readings)
    assert memories["remedies"] == ["cleanup"]
    assert "upgrade" not in memories["help"].lower()
    assert per_day["remedies"] == ["wait", "upgrade"]


@pytest.mark.parametrize("plan", list(PLAN_ORDER))
def test_help_is_short_names_the_failing_call_and_has_no_url(plan):
    readings = {
        MEMORIES: _reading(10, 10, plan=plan),
        MEMORIES_PER_DAY: _reading(10, 10, plan=plan, resets_at=RESETS_AT),
        MCP_CALLS_PER_DAY: _reading(10, 10, plan=plan, resets_at=RESETS_AT),
    }
    memories, per_day, calls = headroom_notices(readings)
    for notice in (memories, per_day, calls):
        text = notice["help"]
        assert len(text) <= 300, (notice["dimension"], len(text))
        assert "http" not in text and "://" not in text and "www." not in text
    assert "remember will be refused" in memories["help"]
    assert "remember will be refused" in per_day["help"]
    assert "MCP tools" in calls["help"]
    assert "forget" in memories["help"]


@pytest.mark.parametrize("plan", [*PLAN_ORDER])
def test_without_billing_the_administrator_is_named_instead_of_upgrade(plan):
    """No billing handoff: the plan page cannot upgrade; PLAN_<KEY>_* is the admin's."""
    readings = {
        MEMORIES: _reading(9, 10, plan=plan),
        MEMORIES_PER_DAY: _reading(9, 10, plan=plan, resets_at=RESETS_AT),
        MCP_CALLS_PER_DAY: _reading(10, 10, plan=plan, resets_at=RESETS_AT),
    }
    notices = headroom_notices(readings, billing=False)
    assert [n["remedies"] for n in notices] == [
        ["cleanup", "admin"],
        ["wait", "admin"],
        ["wait", "admin"],
    ]
    for notice in notices:
        assert "administrator" in notice["help"]
        assert "upgrade" not in notice["help"].lower()
        assert len(notice["help"]) <= 300 and "http" not in notice["help"]


async def test_plan_headroom_reads_the_billing_switch(first_this_hour):
    counts = _counts(**{MEMORIES: _reading(8, 10, plan="free")})
    with patch.object(ph, "billing_configured", return_value=False):
        (notice,) = await plan_headroom(MagicMock(), workspace_id=None, counts=counts)
    assert notice["remedies"] == ["cleanup", "admin"]
    with patch.object(ph, "billing_configured", return_value=True):
        first_this_hour.return_value = True
        (notice,) = await plan_headroom(MagicMock(), workspace_id=None, counts=counts)
    assert notice["remedies"] == ["cleanup", "upgrade"]


def test_billing_configured_is_the_plan_pages_switch():
    signer = MagicMock(is_configured=True)
    with patch("auth.billing_handoff.BillingHandoffSigner", return_value=signer):
        assert ph.billing_configured() is True
    signer.is_configured = False
    with patch("auth.billing_handoff.BillingHandoffSigner", return_value=signer):
        assert ph.billing_configured() is False


def test_upgrade_help_names_the_relative_plan_page():
    (notice,) = headroom_notices({MEMORIES: _reading(9, 10, plan="free")})
    assert "Workspace settings → Plan" in notice["help"]


def test_notices_follow_the_dimension_order():
    readings = {
        MCP_CALLS_PER_DAY: _reading(9, 10, resets_at=RESETS_AT),
        MEMORIES: _reading(9, 10),
        MEMORIES_PER_DAY: _reading(1, 10, resets_at=RESETS_AT),
    }
    assert [n["dimension"] for n in headroom_notices(readings)] == [MEMORIES, MCP_CALLS_PER_DAY]


# ------------------------------------------------------------ the collector


def test_a_reading_outside_a_collector_is_dropped():
    record_quota_reading(MEMORIES, _reading(1, 10))  # no error, nothing kept


def test_the_collector_keeps_the_latest_reading_and_resets():
    with collect_plan_headroom_counts() as counts:
        record_quota_reading(MEMORIES, _reading(1, 10))
        record_quota_reading(MEMORIES, _reading(2, 10))
    assert counts.readings[MEMORIES].used == 2
    record_quota_reading(MEMORIES, _reading(3, 10))
    assert counts.readings[MEMORIES].used == 2


def test_the_dispatcher_seeds_the_collector_with_its_mcp_count():
    reading = QuotaReading(WS, 80, 100, None, RESETS_AT)
    token = open_plan_headroom_counts(reading)
    try:
        counts = current_plan_headroom_counts()
        assert counts is not None and counts.readings == {MCP_CALLS_PER_DAY: reading}
        record_quota_reading(MEMORIES, _reading(1, 10))
        assert set(counts.readings) == {MCP_CALLS_PER_DAY, MEMORIES}
    finally:
        close_plan_headroom_counts(token)
    assert current_plan_headroom_counts() is None


def test_drop_quota_reading_forces_a_recount():
    with collect_plan_headroom_counts() as counts:
        record_quota_reading(MEMORIES, _reading(9, 10))
        drop_quota_reading(MEMORIES)
        drop_quota_reading(MEMORIES)  # absent: no error
    assert MEMORIES not in counts.readings


def test_mcp_calls_reading_counts_this_call():
    """The dispatcher's ``used`` is the calls before this one."""
    with patch.object(ph, "utcnow", return_value=NOW):
        reading = mcp_calls_reading(WS, 79, 100)
    assert (reading.used, reading.limit, reading.plan) == (80, 100, None)
    assert reading.resets_at == RESETS_AT


# ------------------------------------------- readings from the quota checks


def _db_returning(workspace) -> MagicMock:
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = workspace
    db.execute = AsyncMock(return_value=result)
    return db


async def test_check_memory_quota_records_the_count_after_this_write():
    from services.quota_service import QuotaService

    ws = SimpleNamespace(id=WS, plan_name="basic")
    with (
        patch("services.quota_service.count_live_memories", new=AsyncMock(return_value=7)),
        patch(
            "services.quota_service.EffectiveQuotaService.get_effective_quotas",
            new=AsyncMock(return_value={"memory_limit": 10}),
        ),
        collect_plan_headroom_counts() as counts,
    ):
        assert await QuotaService(_db_returning(ws)).check_memory_quota(WS) == (True, None)
    assert counts.readings[MEMORIES] == QuotaReading(WS, 8, 10, "basic")


async def test_a_refused_memory_quota_records_the_count_as_it_stands():
    from services.quota_service import QuotaService

    ws = SimpleNamespace(id=WS, plan_name="free")
    with (
        patch("services.quota_service.count_live_memories", new=AsyncMock(return_value=10)),
        patch(
            "services.quota_service.EffectiveQuotaService.get_effective_quotas",
            new=AsyncMock(return_value={"memory_limit": 10}),
        ),
        collect_plan_headroom_counts() as counts,
    ):
        allowed, _ = await QuotaService(_db_returning(ws)).check_memory_quota(WS)
    assert allowed is False
    assert counts.readings[MEMORIES].used == 10


async def test_check_memories_per_day_records_the_reserved_total():
    from services.quota_service import QuotaService

    ws = SimpleNamespace(id=WS, plan_name="free", effective_memories_per_day=50)
    with (
        patch("services.quota_service.utcnow", return_value=NOW),
        patch("services.quota_service.incrby_counter", new=AsyncMock(return_value=41)),
        collect_plan_headroom_counts() as counts,
    ):
        assert await QuotaService(_db_returning(ws)).check_memories_per_day(WS, count=3) == (
            True,
            None,
        )
    assert counts.readings[MEMORIES_PER_DAY] == QuotaReading(WS, 41, 50, "free", RESETS_AT)


async def test_a_refused_day_records_the_count_after_the_refund():
    from services.quota_service import QuotaService

    ws = SimpleNamespace(id=WS, plan_name="free", effective_memories_per_day=50)
    with (
        patch("services.quota_service.utcnow", return_value=NOW),
        patch("services.quota_service.incrby_counter", new=AsyncMock(side_effect=[51, 50])),
        collect_plan_headroom_counts() as counts,
    ):
        allowed, _ = await QuotaService(_db_returning(ws)).check_memories_per_day(WS)
    assert allowed is False
    assert counts.readings[MEMORIES_PER_DAY].used == 50


async def test_an_unreachable_day_counter_records_nothing():
    from services.quota_service import QuotaService

    ws = SimpleNamespace(id=WS, plan_name="free", effective_memories_per_day=50)
    with (
        patch("services.quota_service.incrby_counter", new=AsyncMock(side_effect=RedisError("x"))),
        collect_plan_headroom_counts() as counts,
    ):
        await QuotaService(_db_returning(ws)).check_memories_per_day(WS)
    assert MEMORIES_PER_DAY not in counts.readings


# ------------------------------------------------------------- the throttle
# (workspace_id=None: the readings are given, nothing is read.)


async def test_a_notice_arms_the_hourly_key(first_this_hour):
    counts = _counts(**{MEMORIES: _reading(8, 10)})
    notices = await plan_headroom(MagicMock(), workspace_id=None, counts=counts)
    assert [n["level"] for n in notices] == ["warning"]
    first_this_hour.assert_awaited_once_with(f"plan_headroom:{WS}:memories:warning", "1", ttl=3600)


async def test_a_second_notice_within_the_hour_is_dropped(first_this_hour):
    first_this_hour.return_value = False
    counts = _counts(**{MEMORIES: _reading(8, 10)})
    assert await plan_headroom(MagicMock(), workspace_id=None, counts=counts) == []


async def test_each_dimension_and_level_has_its_own_key(first_this_hour):
    first_this_hour.side_effect = lambda key, *_a, **_k: key.endswith(":critical")
    counts = _counts(
        **{
            MEMORIES: _reading(8, 10),  # warning, already shown this hour
            MEMORIES_PER_DAY: _reading(10, 10, resets_at=RESETS_AT),  # critical, new
        }
    )
    notices = await plan_headroom(MagicMock(), workspace_id=None, counts=counts)
    assert [(n["dimension"], n["level"]) for n in notices] == [(MEMORIES_PER_DAY, "critical")]
    keys = [c.args[0] for c in first_this_hour.await_args_list]
    assert keys == [
        f"plan_headroom:{WS}:memories:warning",
        f"plan_headroom:{WS}:memories_per_day:critical",
    ]


async def test_unthrottled_calls_neither_read_nor_arm_the_key(first_this_hour):
    """bootstrap: a session start always carries the notice."""
    first_this_hour.return_value = False
    counts = _counts(**{MEMORIES: _reading(8, 10)})
    notices = await plan_headroom(MagicMock(), workspace_id=None, counts=counts, throttle=False)
    assert len(notices) == 1
    first_this_hour.assert_not_awaited()


async def test_redis_failure_fails_open_with_a_warning(first_this_hour):
    first_this_hour.side_effect = RedisError("redis down")
    counts = _counts(**{MEMORIES: _reading(8, 10)})
    with patch.object(ph, "logger") as log:
        notices = await plan_headroom(MagicMock(), workspace_id=None, counts=counts)
    assert len(notices) == 1
    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "plan_headroom_throttle_unavailable"


async def test_below_the_thresholds_redis_is_not_touched(first_this_hour):
    counts = _counts(**{MEMORIES: _reading(1, 10)})
    assert await plan_headroom(MagicMock(), workspace_id=None, counts=counts) == []
    first_this_hour.assert_not_awaited()


# --------------------------------------------------- completing the readings


def _workspace(plan="basic", memory_limit=10, per_day=10, calls=10):
    return SimpleNamespace(
        id=WS,
        plan_name=plan,
        effective_memory_limit=memory_limit,
        effective_memories_per_day=per_day,
        effective_mcp_calls_per_day=calls,
    )


class _Reads:
    """The get_usage-style reads, recorded."""

    def __init__(self, memories=0, today=0, calls=0):
        self.live = AsyncMock(return_value=memories)
        self.today = AsyncMock(return_value=today)
        self.calls = AsyncMock(return_value=calls)

    def patches(self):
        return (
            patch.object(ph, "count_live_memories", new=self.live),
            patch(
                "services.quota_service.QuotaService.count_memories_created_today", new=self.today
            ),
            patch("services.quota_service.QuotaService.count_mcp_calls_today", new=self.calls),
        )


async def test_recorded_readings_need_no_read(first_this_hour):
    """The remember path: every dimension came from the write's own checks."""
    db = MagicMock()
    db.get = AsyncMock()
    reads = _Reads()
    counts = _counts(
        **{
            MEMORIES: _reading(8, 10),
            MEMORIES_PER_DAY: _reading(1, 10, resets_at=RESETS_AT),
            # The dispatcher's count, recorded without the workspace row.
            MCP_CALLS_PER_DAY: QuotaReading(WS, 9, 10, None, RESETS_AT),
        }
    )
    p1, p2, p3 = reads.patches()
    with p1, p2, p3:
        notices = await plan_headroom(db, workspace_id=WS, session_workspace_id=WS, counts=counts)
    assert [n["dimension"] for n in notices] == [MEMORIES, MCP_CALLS_PER_DAY]
    # The MCP count was recorded without a plan: the memories reading names it.
    assert notices[1]["plan"] == "free"
    db.get.assert_not_awaited()
    reads.live.assert_not_awaited()
    reads.today.assert_not_awaited()
    reads.calls.assert_not_awaited()


async def test_missing_dimensions_are_read_once(first_this_hour):
    """update_memory in place / bootstrap: nothing was recorded."""
    db = MagicMock()
    db.get = AsyncMock(return_value=_workspace())
    reads = _Reads(memories=8, today=9, calls=10)
    p1, p2, p3 = reads.patches()
    with p1, p2, p3, patch.object(ph, "utcnow", return_value=NOW):
        notices = await plan_headroom(db, workspace_id=WS, session_workspace_id=WS, counts=None)
    assert [(n["dimension"], n["used"], n["level"]) for n in notices] == [
        (MEMORIES, 8, "warning"),
        (MEMORIES_PER_DAY, 9, "warning"),
        (MCP_CALLS_PER_DAY, 10, "critical"),
    ]
    assert notices[1]["resets_at"] == RESETS_AT and notices[2]["resets_at"] == RESETS_AT
    assert all(n["plan"] == "basic" for n in notices)
    db.get.assert_awaited_once()  # one workspace row for all three
    reads.live.assert_awaited_once()
    reads.today.assert_awaited_once()
    reads.calls.assert_awaited_once()


async def test_a_zero_limit_dimension_is_not_read(first_this_hour):
    db = MagicMock()
    db.get = AsyncMock(return_value=_workspace(memory_limit=0, per_day=0, calls=0))
    reads = _Reads()
    p1, p2, p3 = reads.patches()
    with p1, p2, p3:
        assert await plan_headroom(db, workspace_id=WS, session_workspace_id=WS) == []
    reads.live.assert_not_awaited()
    reads.today.assert_not_awaited()
    reads.calls.assert_not_awaited()


async def test_the_mcp_plan_of_another_workspace_is_looked_up(first_this_hour):
    """A context of another workspace: the MCP cap is the session workspace's."""
    session_ws = uuid4()
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id=session_ws, plan_name="pro"))
    counts = _counts(
        **{
            MEMORIES: _reading(1, 10),
            MEMORIES_PER_DAY: _reading(1, 10, resets_at=RESETS_AT),
            MCP_CALLS_PER_DAY: QuotaReading(session_ws, 9, 10, None, RESETS_AT),
        }
    )
    notices = await plan_headroom(
        db, workspace_id=WS, session_workspace_id=session_ws, counts=counts
    )
    (notice,) = notices
    assert (notice["dimension"], notice["plan"]) == (MCP_CALLS_PER_DAY, "pro")
    first_this_hour.assert_awaited_once_with(
        f"plan_headroom:{session_ws}:mcp_calls_per_day:warning", "1", ttl=3600
    )


async def test_an_addon_bonus_makes_the_notice_recede(first_this_hour):
    """The effective limit already holds the add-on: 8 of 10 warns, 8 of 20 does not."""
    db = MagicMock()
    reads = _Reads(memories=8)
    p1, p2, p3 = reads.patches()
    with p1, p2, p3:
        db.get = AsyncMock(return_value=_workspace(memory_limit=10, per_day=0, calls=0))
        assert len(await plan_headroom(db, workspace_id=WS)) == 1
        db.get = AsyncMock(return_value=_workspace(memory_limit=20, per_day=0, calls=0))
        assert await plan_headroom(db, workspace_id=WS) == []
