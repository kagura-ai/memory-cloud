"""The plan headroom notice against Postgres (#2615).

Each call runs through ``execute_tool_call`` — the rate limit, the capacity
gate, the real handlers and the real quota checks — with a fresh session per
``get_db()`` as in production. The tier's limits are made small by replacing
the ``basic`` entry of ``PLAN_TIERS``, so ``Workspace.effective_*`` (tier +
add-on and referral bonuses) is what the notice measures. Redis (the day
counter and the throttle) is an in-memory fake; the embedding pipeline and the
member-seat check are stubbed.

Covered: ``memories`` at 80 % on remember and on bootstrap, ``critical`` at
95 %, nothing below 80 %, the throttle (a second write within the hour is
quiet, one after the key expired is not, bootstrap is never throttled), the
in-place ``update_memory`` read, ``memories_per_day`` and ``mcp_calls_per_day``
with ``resets_at``, an add-on raising the headroom, and that the notice adds no
SQL statement to ``remember``.
Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis as fakeaioredis
import pytest
import pytest_asyncio
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import db.redis as redis_mod
from config.plan_tiers import PLAN_TIERS, PlanName
from utils.datetime import next_utc_midnight_iso, utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

TIER = PlanName.BASIC  # a tier with a higher one: remedies carry "upgrade"


@dataclass
class World:
    workspace_id: uuid.UUID
    context_id: uuid.UUID
    owner: str
    maker: async_sessionmaker[AsyncSession]
    redis: Any


def _limits(memory_limit: int = 1000, memories_per_day: int = 1000, mcp: int = 10_000):
    tier = dataclasses.replace(
        PLAN_TIERS[TIER],
        memory_limit=memory_limit,
        memories_per_day=memories_per_day,
        mcp_calls_per_day=mcp,
    )
    return patch.dict(PLAN_TIERS, {TIER: tier})


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine: AsyncEngine, monkeypatch) -> AsyncIterator[World]:
    from mcp_server.tools import invalidate_rate_limit_cache
    from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole

    tag = uuid.uuid4().hex[:8]
    owner = f"owner-{tag}"
    ws_id, ctx_id = uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
        await session.flush()
        session.add(
            Workspace(
                id=ws_id,
                name=f"ws-{tag}",
                plan_name=str(TIER),
                owner_user_id=owner,
                daily_api_limit=5000,
                weekly_api_limit=25000,
            )
        )
        await session.flush()
        session.add(WorkspaceMember(workspace_id=ws_id, user_id=owner, role=WorkspaceRole.OWNER))
        session.add(
            Context(
                id=ctx_id,
                workspace_id=ws_id,
                name=f"headroom-{tag}",
                created_by=owner,
                is_private=False,
                trust_tier="trusted",
            )
        )
        await session.commit()

    async def _get_db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    fake = fakeaioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis_client", fake)
    invalidate_rate_limit_cache()
    try:
        with (
            patch("db.base.get_db", new=_get_db),
            patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
            patch("db.qdrant.update_memory_payload_in_qdrant", AsyncMock()),
            patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()),
            patch("services.plan_seats.ensure_member_seated", AsyncMock()),
            patch("services.permission_service.ensure_member_seated", AsyncMock()),
            patch("services.context_service.ensure_member_seated", AsyncMock()),
        ):
            yield World(ws_id, ctx_id, owner, maker, fake)
    finally:
        invalidate_rate_limit_cache()
        async with maker() as session:
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()


async def _seed_memories(world: World, n: int) -> list[uuid.UUID]:
    from models.memory import Memory

    ids = [uuid.uuid4() for _ in range(n)]
    async with world.maker() as session:
        session.add_all(
            Memory(
                id=memory_id,
                user_id=world.owner,
                workspace_id=world.workspace_id,
                context_id=world.context_id,
                summary=f"seeded memory {memory_id.hex[:6]}",
                content="x",
                type="note",
                client="test",
                scope="working",
            )
            for memory_id in ids
        )
        await session.commit()
    return ids


async def _call(world: World, tool: str, **arguments: Any) -> dict[str, Any]:
    from mcp_server.tools import execute_tool_call

    (block,) = await execute_tool_call(
        tool,
        {"context_id": str(world.context_id), **arguments},
        world.owner,
        world.workspace_id,
    )
    return json.loads(block.text)


async def _remember(world: World) -> dict[str, Any]:
    return await _call(
        world,
        "remember",
        summary=f"headroom write {uuid.uuid4().hex[:8]} about the plan limit",
        content="body",
        type="note",
    )


def _key(world: World, dimension: str, level: str) -> str:
    return f"plan_headroom:{world.workspace_id}:{dimension}:{level}"


async def test_memories_dimension_and_the_throttle(world: World) -> None:
    ids = await _seed_memories(world, 7)
    with _limits(memory_limit=10):
        first = await _remember(world)
        assert first["status"] == "success", first
        (notice,) = first["plan_headroom"]
        assert notice == {
            "dimension": "memories",
            "level": "warning",
            "used": 8,
            "limit": 10,
            "percentage": 80.0,
            "plan": "basic",
            "remedies": ["cleanup", "upgrade"],
            "help": notice["help"],
        }
        assert "http" not in notice["help"]
        assert 0 < await world.redis.ttl(_key(world, "memories", "warning")) <= 3600

        # A session start always carries it, throttle or not.
        boot = await _call(world, "bootstrap")
        assert [(n["dimension"], n["used"]) for n in boot["plan_headroom"]] == [("memories", 8)]

        # 9 of 10: still warning, shown within the hour already.
        second = await _remember(world)
        assert second["status"] == "success", second
        assert "plan_headroom" not in second

        # The hour passed: an in-place edit (no quota check ran) reads the count.
        await world.redis.delete(_key(world, "memories", "warning"))
        edited = await _call(
            world, "update_memory", memory_id=str(ids[0]), summary="seeded memory, edited"
        )
        assert edited["status"] == "success", edited
        assert [(n["level"], n["used"]) for n in edited["plan_headroom"]] == [("warning", 9)]

        # 10 of 10: critical has its own key and is shown at once.
        third = await _remember(world)
        assert [(n["level"], n["used"]) for n in third["plan_headroom"]] == [("critical", 10)]
        boot = await _call(world, "bootstrap")
        assert [n["level"] for n in boot["plan_headroom"]] == ["critical"]

        # And the next write is the refusal the notice announced.
        refused = await _remember(world)
        assert refused["error"] == "quota_exceeded"
        assert "plan_headroom" not in refused


async def test_below_80_percent_the_field_is_absent(world: World) -> None:
    await _seed_memories(world, 6)
    with _limits(memory_limit=10):
        reply = await _remember(world)  # 7 of 10
        assert reply["status"] == "success", reply
        assert "plan_headroom" not in reply
        boot = await _call(world, "bootstrap")
        assert "plan_headroom" not in boot
    assert await world.redis.keys("plan_headroom:*") == []


async def test_an_addon_bonus_raises_the_headroom(world: World) -> None:
    from models.auth import Workspace

    await _seed_memories(world, 8)
    async with world.maker() as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == world.workspace_id)
            .values(addon_memory_bonus=10)
        )
        await session.commit()
    with _limits(memory_limit=10):
        reply = await _remember(world)  # 9 of 10 + 10
        assert reply["status"] == "success", reply
        assert "plan_headroom" not in reply


async def test_memories_per_day_carries_resets_at(world: World) -> None:
    today = utcnow().date()
    await world.redis.set(f"quota:workspace:{world.workspace_id}:memories:{today}", 7)
    with _limits(memories_per_day=10):
        first = await _remember(world)  # the 8th today
        (notice,) = first["plan_headroom"]
        assert (notice["dimension"], notice["level"], notice["used"]) == (
            "memories_per_day",
            "warning",
            8,
        )
        assert notice["resets_at"] == next_utc_midnight_iso(today)
        assert notice["remedies"] == ["wait", "upgrade"]

        assert "plan_headroom" not in await _remember(world)  # 9th: throttled
        third = await _remember(world)  # 10th: critical
        assert [(n["level"], n["used"]) for n in third["plan_headroom"]] == [("critical", 10)]


async def test_mcp_calls_per_day_carries_resets_at(world: World) -> None:
    from models.auth import UsageStats

    today = utcnow().date()
    async with world.maker() as session:
        session.add_all(
            UsageStats(
                user_id=world.owner,
                endpoint="mcp:recall",
                method="MCP",
                status_code=200,
                date=today,
                workspace_id=world.workspace_id,
            )
            for _ in range(7)
        )
        await session.commit()
    with _limits(mcp=10):
        reply = await _remember(world)  # the 8th call today
        (notice,) = reply["plan_headroom"]
        assert (notice["dimension"], notice["level"], notice["used"], notice["limit"]) == (
            "mcp_calls_per_day",
            "warning",
            8,
            10,
        )
        assert notice["resets_at"] == next_utc_midnight_iso(today)
        assert notice["plan"] == "basic"


async def _statements_of_one_remember(
    world: World, async_engine: AsyncEngine
) -> tuple[int, dict[str, Any]]:
    """SQL statements one remember issues, the dispatcher's rate-limit COUNT included."""
    from mcp_server.tools import invalidate_rate_limit_cache
    from models.auth import Context

    async with world.maker() as session:  # same context-touch work on every run
        await session.execute(
            update(Context).where(Context.id == world.context_id).values(last_used_at=None)
        )
        await session.commit()
    invalidate_rate_limit_cache()
    seen: list[str] = []

    def count(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", count)
    try:
        reply = await _remember(world)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", count)
    assert reply["status"] == "success", reply
    return len(seen), reply


async def test_the_notice_adds_no_statement_to_remember(world: World, async_engine) -> None:
    """The numbers come from the write's own quota checks and the rate limit."""
    await _seed_memories(world, 6)
    with _limits(memory_limit=10, memories_per_day=10, mcp=10):
        await _remember(world)  # warm-up: 7 of 10
        with patch("services.plan_headroom.plan_headroom", new=AsyncMock(return_value=[])):
            without, quiet = await _statements_of_one_remember(world, async_engine)  # 8 of 10
        await world.redis.flushall()  # no throttle key, no day counter
        with_notice, shown = await _statements_of_one_remember(world, async_engine)  # 9 of 10
    assert "plan_headroom" not in quiet
    assert [n["dimension"] for n in shown["plan_headroom"]] == ["memories"]
    assert with_notice == without
