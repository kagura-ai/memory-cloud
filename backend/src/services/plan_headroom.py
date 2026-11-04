"""Plan headroom notice on the MCP write responses and bootstrap (#2615).

An MCP client used to learn about a plan limit only when it was hit:
``remember`` answers ``quota_exceeded`` once the live memory count or the
day's creations reach the cap, and every metered tool answers
``rate_limit_exceeded`` once the day's MCP calls are used up. The success
envelope of ``remember``, ``remember_batch``, ``update_memory`` and
``bootstrap`` now carries ``plan_headroom``: one entry per dimension at or
above 80 % (``warning``, the flag ``QuotaService.get_quota_status`` sets) or
95 % (``critical``) of its *effective* limit. The effective limits already
hold the add-on and referral bonuses (``Workspace.effective_*``), so an
add-on raises the headroom and the notice recedes on the next call with
nothing to reset.

Where the numbers come from: the MCP dispatcher opens one
:class:`PlanHeadroomCounts` per tool call (``open_plan_headroom_counts``,
next to the capacity policy it sets) and puts the day's MCP calls its rate
limit just counted in it; the quota checks the write runs record what they
read into it (``record_quota_reading``, a no-op outside a tool call). A
``remember`` therefore issues no extra statement for the notice. A dimension
nothing recorded — an in-place ``update_memory``, ``bootstrap`` — is read
once, the way ``get_usage`` reads it.

Throttle: at most one notice per (workspace, dimension, level) per hour, a
Redis key ``plan_headroom:<workspace_id>:<dimension>:<level>`` set with NX
and a 3600 s TTL, so a busy session sees it at the crossing and then hourly.
``bootstrap`` neither reads nor arms the key (a session start always carries
the notice). A Redis failure fails open: the notice is shown.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

from config.plan_tiers import PLAN_ORDER, plan_rank
from db.redis import set_if_absent
from services.memory_counts import count_live_memories
from utils.datetime import next_utc_midnight_iso, utcnow
from utils.exceptions import RedisError
from utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

MEMORIES = "memories"
MEMORIES_PER_DAY = "memories_per_day"
MCP_CALLS_PER_DAY = "mcp_calls_per_day"
# Wire order of the entries.
DIMENSIONS = (MEMORIES, MEMORIES_PER_DAY, MCP_CALLS_PER_DAY)

WARNING = "warning"
CRITICAL = "critical"
WARNING_PERCENT = 80
CRITICAL_PERCENT = 95
THROTTLE_TTL_SECONDS = 3600

# Remedies, in the order the help text names them. Deleting memories frees
# the total cap only: the day counters count creations and calls, which a
# forget does not give back, so their remedy is the reset. Add-ons are not on
# sale; when they are, an "addon" remedy joins here behind a plan-feature or
# settings switch.
_CLEANUP = "cleanup"
_WAIT = "wait"
_UPGRADE = "upgrade"
_REMEDY_TEXT = {
    _CLEANUP: (
        "free capacity: forget memories the user no longer needs (memory_ids takes up to 50) "
        "or delete unused contexts"
    ),
    _WAIT: "wait for the daily reset at {resets_at}",
    _UPGRADE: (
        "upgrade the plan from the workspace plan page in the web UI (Workspace settings → Plan)"
    ),
}
# What starts failing at 100 %.
_AT_LIMIT = {
    MEMORIES: "At 100 % remember will be refused.",
    MEMORIES_PER_DAY: "At 100 % remember will be refused until the reset.",
    MCP_CALLS_PER_DAY: (
        "At 100 % the metered MCP tools (remember, recall, …) will be refused until the reset."
    ),
}


@dataclass(frozen=True)
class QuotaReading:
    """One dimension as a quota check read it, counting the write it gates.

    Attributes:
        workspace_id: The workspace whose cap this is (the row's workspace for
            the memory caps, the session's for the MCP calls).
        used: The count once this write has landed.
        limit: The effective limit (tier + add-on and referral bonuses).
        plan: The workspace's plan name; ``None`` when the recorder did not
            have the row (the dispatcher's MCP count) — resolved later.
        resets_at: Next UTC midnight for the daily dimensions.
    """

    workspace_id: UUID
    used: int
    limit: int
    plan: str | None = None
    resets_at: str | None = None


@dataclass
class PlanHeadroomCounts:
    """The readings one MCP write collected, by dimension (the last one wins)."""

    readings: dict[str, QuotaReading] = field(default_factory=dict)


_collector: ContextVar[PlanHeadroomCounts | None] = ContextVar("plan_headroom_counts", default=None)


def _new_counts(mcp_calls: QuotaReading | None) -> PlanHeadroomCounts:
    counts = PlanHeadroomCounts()
    if mcp_calls is not None:
        counts.readings[MCP_CALLS_PER_DAY] = mcp_calls
    return counts


def open_plan_headroom_counts(
    mcp_calls: QuotaReading | None = None,
) -> Token[PlanHeadroomCounts | None]:
    """Start the running tool call's collector; pass the token to ``close_plan_headroom_counts``.

    The object is shared, not copied, with the tasks the call starts
    (``execute_with_timeout``), so a quota check running in one records into
    it.

    Args:
        mcp_calls: The day's MCP calls the dispatcher's rate limit counted
            (``mcp_calls_reading``), or None for an exempt tool.
    """
    return _collector.set(_new_counts(mcp_calls))


def close_plan_headroom_counts(token: Token[PlanHeadroomCounts | None]) -> None:
    """Restore the collector that was in effect before ``open_plan_headroom_counts``."""
    _collector.reset(token)


@contextmanager
def collect_plan_headroom_counts(
    mcp_calls: QuotaReading | None = None,
) -> Iterator[PlanHeadroomCounts]:
    """``open_plan_headroom_counts`` / ``close_plan_headroom_counts`` as a block."""
    counts = _new_counts(mcp_calls)
    token = _collector.set(counts)
    try:
        yield counts
    finally:
        _collector.reset(token)


def current_plan_headroom_counts() -> PlanHeadroomCounts | None:
    """The running tool call's collector; None outside the dispatcher."""
    return _collector.get()


def record_quota_reading(dimension: str, reading: QuotaReading) -> None:
    """Keep ``reading`` for the running tool call; a no-op anywhere else.

    Several writes in one call (``remember_batch``): the last reading wins.
    """
    counts = _collector.get()
    if counts is not None:
        counts.readings[dimension] = reading


def drop_quota_reading(dimension: str) -> None:
    """Forget a recorded reading the call made stale, so the notice reads it again.

    ``update_memory``'s external_id replacement records remember's count + 1
    and then deletes the row it replaced: the live count did not grow.
    """
    counts = _collector.get()
    if counts is not None:
        counts.readings.pop(dimension, None)


def mcp_calls_reading(workspace_id: UUID, used_before: int, limit: int) -> QuotaReading:
    """The day's MCP calls once this call is counted.

    Args:
        workspace_id: The session workspace the rate limit metered.
        used_before: The rate-limit check's count, which excludes this call.
        limit: ``effective_mcp_calls_per_day``.
    """
    return QuotaReading(
        workspace_id=workspace_id,
        used=used_before + 1,
        limit=limit,
        resets_at=next_utc_midnight_iso(utcnow().date()),
    )


def _level(reading: QuotaReading) -> str | None:
    """``critical`` at 95 %, ``warning`` at 80 %, else None; a limit of 0 never."""
    if reading.limit <= 0:
        return None
    if reading.used * 100 >= reading.limit * CRITICAL_PERCENT:
        return CRITICAL
    if reading.used * 100 >= reading.limit * WARNING_PERCENT:
        return WARNING
    return None


def _remedies(dimension: str, plan: str | None) -> list[str]:
    remedies = [_CLEANUP if dimension == MEMORIES else _WAIT]
    if plan_rank(plan) < len(PLAN_ORDER) - 1:
        remedies.append(_UPGRADE)
    return remedies


def _help(dimension: str, remedies: list[str], resets_at: str | None) -> str:
    phrases = [_REMEDY_TEXT[r].format(resets_at=resets_at or "UTC midnight") for r in remedies]
    text = ", or ".join(phrases)
    return f"{text[0].upper()}{text[1:]}. {_AT_LIMIT[dimension]}"


def _notice(dimension: str, reading: QuotaReading) -> dict[str, Any] | None:
    level = _level(reading)
    if level is None:
        return None
    remedies = _remedies(dimension, reading.plan)
    return {
        "dimension": dimension,
        "level": level,
        "used": reading.used,
        "limit": reading.limit,
        "percentage": round(reading.used * 100 / reading.limit, 1),
        **({"resets_at": reading.resets_at} if reading.resets_at else {}),
        **({"plan": reading.plan} if reading.plan else {}),
        "remedies": remedies,
        "help": _help(dimension, remedies, reading.resets_at),
    }


def headroom_notices(readings: Mapping[str, QuotaReading]) -> list[dict[str, Any]]:
    """The entries for the dimensions at or above 80 %, in wire order.

    Pure over the numbers. Each entry is ``{dimension, level, used, limit,
    percentage, resets_at?, plan, remedies, help}``.
    """
    notices = []
    for dimension in DIMENSIONS:
        reading = readings.get(dimension)
        notice = _notice(dimension, reading) if reading is not None else None
        if notice is not None:
            notices.append(notice)
    return notices


def throttle_key(workspace_id: UUID, dimension: str, level: str) -> str:
    """The hourly throttle key of one (workspace, dimension, level)."""
    return f"plan_headroom:{workspace_id}:{dimension}:{level}"


async def _first_this_hour(workspace_id: UUID, dimension: str, level: str) -> bool:
    """Arm the throttle key; True when it was free. Fails open on a Redis error."""
    key = throttle_key(workspace_id, dimension, level)
    try:
        return await set_if_absent(key, "1", ttl=THROTTLE_TTL_SECONDS)
    except RedisError as e:
        logger.warning("plan_headroom_throttle_unavailable", key=key, error=str(e))
        return True


async def _complete_readings(
    db: AsyncSession,
    workspace_id: UUID | None,
    session_workspace_id: UUID | None,
    counts: PlanHeadroomCounts | None,
) -> dict[str, QuotaReading]:
    """The recorded readings plus a read of each dimension nothing recorded.

    Reads, at most once each and only for a dimension with a limit: the
    workspace row (``AsyncSession.get``, free when the request already loaded
    it), the live memory COUNT, the day counter GET and the MCP-call COUNT.
    A reading recorded without a plan takes the plan of a reading or row of
    the same workspace; the row is read only when that reading crosses a
    threshold.
    """
    from models.auth import Workspace
    from services.quota_service import QuotaService

    readings = dict(counts.readings) if counts is not None else {}
    rows: dict[UUID, Any] = {}

    async def row(ws_id: UUID) -> Any:
        if ws_id not in rows:
            rows[ws_id] = await db.get(Workspace, ws_id)
        return rows[ws_id]

    today = utcnow().date()
    resets_at = next_utc_midnight_iso(today)
    if workspace_id is not None and not {MEMORIES, MEMORIES_PER_DAY} <= readings.keys():
        ws = await row(workspace_id)
        if ws is not None:
            if MEMORIES not in readings and ws.effective_memory_limit > 0:
                readings[MEMORIES] = QuotaReading(
                    workspace_id,
                    await count_live_memories(db, workspace_id),
                    ws.effective_memory_limit,
                    ws.plan_name,
                )
            if MEMORIES_PER_DAY not in readings and ws.effective_memories_per_day > 0:
                readings[MEMORIES_PER_DAY] = QuotaReading(
                    workspace_id,
                    await QuotaService(db).count_memories_created_today(workspace_id, today=today),
                    ws.effective_memories_per_day,
                    ws.plan_name,
                    resets_at,
                )
    if MCP_CALLS_PER_DAY not in readings and session_workspace_id is not None:
        ws = await row(session_workspace_id)
        if ws is not None and ws.effective_mcp_calls_per_day > 0:
            readings[MCP_CALLS_PER_DAY] = QuotaReading(
                session_workspace_id,
                await QuotaService(db).count_mcp_calls_today(session_workspace_id),
                ws.effective_mcp_calls_per_day,
                ws.plan_name,
                resets_at,
            )

    plans = {r.workspace_id: r.plan for r in readings.values() if r.plan}
    for dimension, reading in list(readings.items()):
        if reading.plan is not None or _level(reading) is None:
            continue
        plan = plans.get(reading.workspace_id)
        if plan is None:
            ws = await row(reading.workspace_id)
            plan = ws.plan_name if ws is not None else None
        readings[dimension] = replace(reading, plan=plan)
    return readings


async def plan_headroom(
    db: AsyncSession,
    *,
    workspace_id: UUID | None,
    session_workspace_id: UUID | None = None,
    counts: PlanHeadroomCounts | None = None,
    throttle: bool = True,
) -> list[dict[str, Any]]:
    """The ``plan_headroom`` entries for one MCP response.

    Args:
        db: The handler's session (after its commit, or before any lane runs).
        workspace_id: The workspace the memory caps belong to — the context's.
        session_workspace_id: The MCP call's workspace, whose MCP-call cap the
            dispatcher metered.
        counts: What the write's quota checks recorded; None reads everything.
        throttle: False for ``bootstrap``: the key is neither read nor armed.

    Returns:
        The entries at or above 80 %, the throttled ones removed; ``[]``
        when there is nothing to say.

    Raises:
        Whatever a read raises; ``_plan_headroom_field`` (the MCP helper)
        turns that into an absent field.
    """
    readings = await _complete_readings(db, workspace_id, session_workspace_id, counts)
    notices = []
    for dimension in DIMENSIONS:
        reading = readings.get(dimension)
        notice = _notice(dimension, reading) if reading is not None else None
        if notice is None:
            continue
        if throttle and not await _first_this_hour(
            reading.workspace_id, dimension, notice["level"]
        ):
            continue
        notices.append(notice)
    return notices
