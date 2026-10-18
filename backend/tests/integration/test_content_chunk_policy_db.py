"""The #2103 content chunk policy against a real Postgres.

The rank SQL (earliest ``content_chunks_enabled_at`` first, then the context
id; a NULL stamp last; deleted contexts out), the cross-context window query,
the downgrade report, the admin plan change that reports and purges nothing,
and the cap's advisory lock under N parallel enables (with a negative control:
the same race without the lock goes over the cap). Skipped when Postgres is
unreachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.auth import Context, Workspace
from models.config import ContextSearchConfig
from services import content_chunk_policy as policy
from utils.exceptions import QuotaExceededError

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "chunk-policy-it"
_T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(
        policy, "get_settings", lambda: SimpleNamespace(content_chunks_require_plan=True)
    )
    monkeypatch.setattr(policy, "chunk_points_supported", lambda: True)


async def _workspace(db: AsyncSession, plan: str) -> UUID:
    ws = Workspace(
        id=uuid4(),
        name=f"ccp-{uuid4().hex[:8]}",
        plan_name=plan,
        owner_user_id=_USER,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    db.add(ws)
    await db.flush()
    return ws.id


async def _context(
    db: AsyncSession,
    ws_id: UUID,
    *,
    on: bool,
    enabled_at: datetime | None = None,
    deleted: bool = False,
    context_id: UUID | None = None,
) -> UUID:
    ctx = Context(
        id=context_id or uuid4(),
        workspace_id=ws_id,
        name=f"ccp-{uuid4().hex[:8]}",
        created_by=_USER,
        deleted_at=func.now() if deleted else None,
    )
    db.add(ctx)
    await db.flush()
    db.add(
        ContextSearchConfig(
            context_id=ctx.id,
            content_chunks_enabled=on,
            content_chunks_enabled_at=enabled_at,
        )
    )
    await db.flush()
    return ctx.id


@pytest_asyncio.fixture(loop_scope="session")
async def cleanup(db_session: AsyncSession) -> AsyncIterator[list[UUID]]:
    workspaces: list[UUID] = []
    yield workspaces
    await db_session.rollback()
    for ws_id in workspaces:
        ctx_ids = select(Context.id).where(Context.workspace_id == ws_id)
        await db_session.execute(
            ContextSearchConfig.__table__.delete().where(
                ContextSearchConfig.context_id.in_(ctx_ids)
            )
        )
        await db_session.execute(Context.__table__.delete().where(Context.workspace_id == ws_id))
        await db_session.execute(Workspace.__table__.delete().where(Workspace.id == ws_id))
    await db_session.commit()


async def test_the_earliest_enabled_stay_active_and_a_null_stamp_ranks_last(
    db_session, cleanup, gated
):
    ws = await _workspace(db_session, "basic")  # cap 1
    cleanup.append(ws)
    first = await _context(db_session, ws, on=True, enabled_at=_T0)
    later = await _context(db_session, ws, on=True, enabled_at=_T0 + timedelta(days=1))
    unstamped = await _context(db_session, ws, on=True, enabled_at=None)
    # A deleted context never takes a slot, however early.
    await _context(db_session, ws, on=True, enabled_at=_T0 - timedelta(days=9), deleted=True)
    off = await _context(db_session, ws, on=False)
    await db_session.commit()

    statuses = {
        cid: await policy.content_chunks_status(db_session, cid, include_usage=True)
        for cid in (first, later, unstamped, off)
    }
    assert [statuses[c].earlier for c in (first, later, unstamped)] == [0, 1, 2]
    assert [statuses[c].effective for c in (first, later, unstamped, off)] == [
        True,
        False,
        False,
        False,
    ]
    assert {s.in_use for s in statuses.values()} == {3}
    assert statuses[later].reason == "cap"
    assert statuses[off].can_enable is False

    active = await policy.content_chunks_effective_ids(db_session, [first, later, unstamped, off])
    assert active == {first}


async def test_equal_stamps_rank_by_context_id(db_session, cleanup, gated):
    ws = await _workspace(db_session, "basic")
    cleanup.append(ws)
    low = UUID("00000000-0000-4000-8000-000000000001")
    high = UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
    await _context(db_session, ws, on=True, enabled_at=_T0, context_id=high)
    await _context(db_session, ws, on=True, enabled_at=_T0, context_id=low)
    await db_session.commit()
    assert await policy.content_chunks_effective_ids(db_session, [low, high]) == {low}
    assert (await policy.content_chunks_status(db_session, low)).effective is True
    assert (await policy.content_chunks_status(db_session, high)).effective is False


async def test_a_plan_without_the_feature_has_none_active(db_session, cleanup, gated):
    ws = await _workspace(db_session, "free")
    cleanup.append(ws)
    ctx = await _context(db_session, ws, on=True, enabled_at=_T0)
    await db_session.commit()
    status = await policy.content_chunks_status(db_session, ctx)
    assert (status.effective, status.reason) == (False, "plan")
    assert await policy.content_chunks_effective_ids(db_session, [ctx]) == set()


async def test_a_downgrade_reports_the_contexts_past_the_new_cap(db_session, cleanup, gated):
    ws = await _workspace(db_session, "pro")
    cleanup.append(ws)
    ids = [
        await _context(db_session, ws, on=True, enabled_at=_T0 + timedelta(hours=i))
        for i in range(3)
    ]
    await db_session.commit()
    assert await policy.contexts_losing_content_chunks(db_session, ws, "pro", "basic") == ids[1:]
    assert await policy.contexts_losing_content_chunks(db_session, ws, "pro", "free") == ids


async def test_the_admin_plan_change_reports_and_keeps_flags_and_points(
    db_session, cleanup, gated, monkeypatch
):
    from api.routes.admin_plans import AdminUpdatePlanRequest, update_workspace_plan

    from ._admin_helpers import mock_admin

    purges: list = []
    monkeypatch.setattr(
        "db.qdrant.purge_context_chunk_points_everywhere",
        lambda *a, **k: purges.append(a),
    )
    monkeypatch.setattr("db.qdrant.delete_chunk_points", lambda *a, **k: purges.append(a))
    ws = await _workspace(db_session, "pro")
    cleanup.append(ws)
    keep = await _context(db_session, ws, on=True, enabled_at=_T0)
    lose = await _context(db_session, ws, on=True, enabled_at=_T0 + timedelta(hours=1))
    await db_session.commit()

    result = await update_workspace_plan(
        workspace_id=str(ws),
        request=AdminUpdatePlanRequest(plan_name="basic"),
        admin_user=mock_admin(),
        db=db_session,
    )

    assert result["content_chunks_deactivated"] == [str(lose)]
    flags = (
        await db_session.execute(
            select(ContextSearchConfig.context_id, ContextSearchConfig.content_chunks_enabled)
            .where(ContextSearchConfig.context_id.in_([keep, lose]))
            .execution_options(populate_existing=True)
        )
    ).all()
    assert dict(flags) == {keep: True, lose: True}
    assert purges == []

    # Upgrading back brings the context back with nothing to do.
    again = await update_workspace_plan(
        workspace_id=str(ws),
        request=AdminUpdatePlanRequest(plan_name="pro"),
        admin_user=mock_admin(),
        db=db_session,
    )
    assert again["content_chunks_deactivated"] == []
    assert (await policy.content_chunks_status(db_session, lose)).effective is True


# ---------------------------------------------------------------------------
# The cap's advisory lock under concurrency
# ---------------------------------------------------------------------------

_CAP = 5  # pro
_ATTEMPTS = 12


@pytest_asyncio.fixture(loop_scope="session")
async def race_world(db_session, cleanup) -> tuple[UUID, list[UUID]]:
    ws = await _workspace(db_session, "pro")
    cleanup.append(ws)
    contexts = [await _context(db_session, ws, on=False) for _ in range(_ATTEMPTS)]
    await db_session.commit()
    return ws, contexts


async def _enable(session: AsyncSession, context_id: UUID) -> None:
    await session.execute(
        update(ContextSearchConfig)
        .where(ContextSearchConfig.context_id == context_id)
        .values(content_chunks_enabled=True, content_chunks_enabled_at=func.now())
    )


async def _count_on(session_maker, ws: UUID) -> int:
    async with session_maker() as session:
        return await policy._count_in_use(session, ws)


async def test_n_parallel_enables_stop_exactly_at_the_cap(async_engine, race_world, gated):
    ws, contexts = race_world
    session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def attempt(context_id: UUID) -> bool:
        async with session_maker() as session:
            async with session.begin():
                try:
                    await policy.ensure_content_chunk_capacity(session, context_id)
                except QuotaExceededError:
                    return False
                await _enable(session, context_id)
                return True

    results = await asyncio.gather(*(attempt(c) for c in contexts), return_exceptions=True)
    assert all(isinstance(r, bool) for r in results), results
    assert sum(results) == _CAP
    assert await _count_on(session_maker, ws) == _CAP


async def test_without_the_lock_the_same_race_goes_over_the_cap(
    async_engine, race_world, gated, monkeypatch
):
    """Negative control: proves the race is real, so the test above cannot
    pass by lucky scheduling."""
    monkeypatch.setattr(policy, "_CAP_LOCK_SQL", text("SELECT CAST(:key AS text)"))
    ws, contexts = race_world
    session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    all_counted = asyncio.Barrier(_ATTEMPTS)

    async def attempt(context_id: UUID) -> bool:
        try:
            async with session_maker() as session:
                async with session.begin():
                    try:
                        await policy.ensure_content_chunk_capacity(session, context_id)
                        refused = False
                    except QuotaExceededError:
                        refused = True
                    await all_counted.wait()
                    if refused:
                        return False
                    await _enable(session, context_id)
                    return True
        except Exception:
            try:
                await all_counted.abort()
            except Exception:
                pass
            raise

    results = await asyncio.gather(*(attempt(c) for c in contexts), return_exceptions=True)
    assert all(isinstance(r, bool) for r in results), results
    assert await _count_on(session_maker, ws) > _CAP
