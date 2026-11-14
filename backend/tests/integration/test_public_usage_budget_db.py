"""The public usage budget on real PostgreSQL (#2805).

Deterministic concurrency and rollover: every outcome below is decided by the
row lock and the charge statement, never by a sleep. The waits on ``pg_locks``
only make sure the contenders are queued before the holder lets go.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import get_settings
from mcp_server.public.tools import PublicArgumentError
from models.auth import Context, Workspace
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from services import public_usage_budget as pub
from services.public_usage_budget import (
    PublicUsageBudgetUnavailable,
    charge_public_usage,
    read_public_usage_budget,
    reset_public_usage_budget,
)
from tests.integration import sync_installation_fixtures as fixtures
from tests.integration.sync_installation_fixtures import (
    activation,
    approve,
    change,
    http,
    paid,
    transfer,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

store = fixtures.store

T0 = datetime(2031, 3, 1, 12, 0, 0)
WEEK = timedelta(days=7)
TICK = timedelta(microseconds=1)


@pytest_asyncio.fixture(loop_scope="session")
async def env(async_engine) -> AsyncIterator[SimpleNamespace]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    created: list[UUID] = []
    tag = uuid4().hex[:8]

    async def workspace() -> UUID:
        wid = uuid4()
        async with factory() as s:
            s.add(Workspace(id=wid, name=f"pub-{tag}-{len(created)}", owner_user_id=f"pub-{tag}"))
            await s.commit()
        created.append(wid)
        return wid

    async def charge(wid: UUID, limit: int, now: datetime | None = None):
        return await charge_public_usage(
            wid, limit=limit, db=SimpleNamespace(bind=async_engine), now=now
        )

    async def row(wid: UUID):
        async with factory() as s:
            return (
                await s.execute(
                    text(
                        "SELECT calls_used, period_started_at, period_seq, updated_at "
                        "FROM public_usage_budgets WHERE workspace_id = :ws"
                    ),
                    {"ws": wid},
                )
            ).one_or_none()

    async def store_row(wid: UUID, *, used: int, start: datetime, seq: int = 1) -> None:
        async with factory() as s:
            await s.execute(
                text(
                    "INSERT INTO public_usage_budgets "
                    "(workspace_id, period_started_at, calls_used, period_seq, updated_at) "
                    "VALUES (:ws, :start, :used, :seq, :start)"
                ),
                {"ws": wid, "start": start, "used": used, "seq": seq},
            )
            await s.commit()

    try:
        yield SimpleNamespace(
            engine=async_engine,
            factory=factory,
            workspace=workspace,
            charge=charge,
            row=row,
            store_row=store_row,
        )
    finally:
        async with factory() as s:
            await s.execute(delete(Workspace).where(Workspace.id.in_(created)))
            await s.commit()


async def _waiting(factory, n: int) -> None:
    """Until ``n`` charge statements wait on a lock (pg_locks, ungranted)."""
    for _ in range(400):
        async with factory() as probe:
            count = (
                await probe.execute(
                    text(
                        "SELECT count(DISTINCT l.pid) FROM pg_locks l "
                        "JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE NOT l.granted "
                        "AND a.query LIKE 'INSERT INTO public_usage_budgets%'"
                    )
                )
            ).scalar_one()
        if count >= n:
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"only {count} of {n} charges queued on the lock")


@pytest.fixture
def patient(monkeypatch):
    """Long lock and charge bounds, for the tests that queue charges on purpose."""
    monkeypatch.setattr(pub, "CHARGE_LOCK_TIMEOUT_MS", 60_000)
    monkeypatch.setattr(pub, "CHARGE_TIMEOUT_SECONDS", 60.0)


# ----------------------------------------------------------------------------
# 1-3: parallel calls cannot overspend
# ----------------------------------------------------------------------------


async def test_parallel_charges_admit_exactly_the_limit(env) -> None:
    wid = await env.workspace()
    limit, extra = 12, 8
    first = await env.charge(wid, limit)
    assert first.admitted and first.calls_used == 1
    results = await asyncio.gather(*(env.charge(wid, limit) for _ in range(limit - 1 + extra)))
    admitted = sorted(r.calls_used for r in results if r.admitted)
    assert admitted == list(range(2, limit + 1))
    refused = [r for r in results if not r.admitted]
    assert len(refused) == extra
    assert all(r.calls_used == limit for r in refused)
    assert all(60 <= r.retry_after_seconds <= 3600 for r in refused)
    assert (await env.row(wid)).calls_used == limit


async def test_forced_contention_admits_only_the_remaining_allowance(env, patient) -> None:
    wid = await env.workspace()
    limit = 10
    await env.store_row(wid, used=limit - 3, start=T0)
    async with env.factory() as holder:
        await holder.execute(
            text("SELECT 1 FROM public_usage_budgets WHERE workspace_id = :ws FOR UPDATE"),
            {"ws": wid},
        )
        tasks = [
            asyncio.ensure_future(env.charge(wid, limit, now=T0 + timedelta(hours=1)))
            for _ in range(7)
        ]
        await _waiting(env.factory, 7)
        assert not any(task.done() for task in tasks)
        await holder.commit()
    results = await asyncio.gather(*tasks)
    assert sorted(r.calls_used for r in results if r.admitted) == [8, 9, 10]
    assert sum(not r.admitted for r in results) == 4
    assert (await env.row(wid)).calls_used == limit


async def test_first_insert_race_makes_one_row_with_the_exact_count(env) -> None:
    wid = await env.workspace()
    limit = 6
    results = await asyncio.gather(*(env.charge(wid, limit) for _ in range(15)))
    assert sorted(r.calls_used for r in results if r.admitted) == list(range(1, limit + 1))
    async with env.factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT calls_used, period_seq FROM public_usage_budgets WHERE workspace_id = :ws"
                ),
                {"ws": wid},
            )
        ).all()
    assert [(r.calls_used, r.period_seq) for r in rows] == [(limit, 1)]


# ----------------------------------------------------------------------------
# 4-5: endpoints of one workspace share it; workspaces are independent
# ----------------------------------------------------------------------------


def _serving(wid: UUID):
    from mcp_server.public.publication import ServingEndpoint

    return ServingEndpoint(
        id=uuid4(),
        public_id="pmcp_" + uuid4().hex[:22],
        workspace_id=wid,
        context_id=uuid4(),
        resource_pk=uuid4(),
        resource_id="catalog",
        public_name="Catalog",
        public_description=None,
        public_usage_guide=None,
        context_name="catalog",
        owner_user_id="owner",
    )


@pytest.fixture
def dispatch_on(monkeypatch, env):
    """The real dispatcher with the switch on: real charge, fake bucket and handlers."""
    from mcp.types import TextContent

    import db.base as db_base
    from mcp_server.public import dispatch
    from mcp_server.public.definitions import PUBLIC_TOOLS
    from mcp_server.public.tools import PublicToolResult
    from mcp_server.tools._helpers import _dumps

    monkeypatch.setattr(db_base, "async_session_factory", env.factory)
    monkeypatch.setattr(dispatch, "check_public_rate_limit", _no_bucket)
    monkeypatch.setattr(dispatch, "record_public_call", lambda record: None)

    async def ok(endpoint, arguments):
        return PublicToolResult(
            [TextContent(type="text", text=_dumps({"status": "success"}))], "success"
        )

    monkeypatch.setattr(dispatch, "PUBLIC_HANDLERS", dict.fromkeys(PUBLIC_TOOLS, ok))
    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_enabled", True)

    async def call(endpoint) -> str:
        import json

        content = await dispatch.call_public_tool(
            endpoint, PUBLIC_TOOLS, "list_tags", {}, era="stateless", session_id=None
        )
        body = json.loads(content[0].text)
        return body.get("error") or body["status"]

    return call


async def _no_bucket(*args, **kwargs):
    return None


async def test_endpoints_and_a_republish_of_one_workspace_share_the_row(
    env, dispatch_on, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_calls", 6)
    wid = await env.workspace()
    first, second, republished = _serving(wid), _serving(wid), _serving(wid)
    outcomes = []
    for endpoint in (first, second, first, second, republished, republished, first, second):
        outcomes.append(await dispatch_on(endpoint))
    assert outcomes == ["success"] * 6 + ["usage_budget_exhausted"] * 2
    assert (await env.row(wid)).calls_used == 6


async def test_two_workspaces_are_independent(env) -> None:
    a, b = await env.workspace(), await env.workspace()
    for _ in range(2):
        assert (await env.charge(a, 2)).admitted
    assert not (await env.charge(a, 2)).admitted
    assert (await env.charge(b, 2)).admitted
    assert (await env.row(a)).calls_used == 2
    assert (await env.row(b)).calls_used == 1


# ----------------------------------------------------------------------------
# 6-9: rollover, clock, refusal writes nothing
# ----------------------------------------------------------------------------


async def test_rollover_with_an_explicit_clock(env) -> None:
    wid = await env.workspace()
    assert (await env.charge(wid, 2, now=T0)).admitted
    assert (await env.charge(wid, 2, now=T0 + timedelta(days=1))).admitted
    refused = await env.charge(wid, 2, now=T0 + WEEK - TICK)
    assert not refused.admitted
    assert refused.retry_after_seconds == 60
    rolled = await env.charge(wid, 2, now=T0 + WEEK)
    assert rolled.admitted
    assert (rolled.calls_used, rolled.period_seq) == (1, 2)
    assert rolled.period_started_at == T0 + WEEK
    # After a 15-day gap the next period starts at the call, not on a grid.
    later = T0 + WEEK + timedelta(days=15, hours=5)
    gap = await env.charge(wid, 2, now=later)
    assert gap.admitted
    assert (gap.calls_used, gap.period_seq, gap.period_started_at) == (1, 3, later)
    stored = await env.row(wid)
    assert (stored.calls_used, stored.period_seq, stored.period_started_at) == (1, 3, later)


async def test_parallel_rollover_starts_exactly_one_period(env) -> None:
    wid = await env.workspace()
    await env.store_row(wid, used=3, start=T0, seq=4)
    results = await asyncio.gather(*(env.charge(wid, 3, now=T0 + WEEK) for _ in range(9)))
    admitted = [r for r in results if r.admitted]
    assert sorted(r.calls_used for r in admitted) == [1, 2, 3]
    assert {r.period_seq for r in admitted} == {5}
    stored = await env.row(wid)
    assert (stored.calls_used, stored.period_seq, stored.period_started_at) == (3, 5, T0 + WEEK)


async def test_clock_stepping_back_continues_the_period(env) -> None:
    wid = await env.workspace()
    await env.store_row(wid, used=1, start=T0, seq=2)
    result = await env.charge(wid, 5, now=T0 - timedelta(hours=1))
    assert result.admitted
    assert (result.calls_used, result.period_seq, result.period_started_at) == (2, 2, T0)


async def test_a_refusal_writes_nothing(env) -> None:
    wid = await env.workspace()
    await env.store_row(wid, used=2, start=T0, seq=3)
    before = await env.row(wid)
    refused = await env.charge(wid, 2, now=T0 + timedelta(hours=1))
    assert not refused.admitted
    assert refused.retry_after_seconds == 3600
    assert await env.row(wid) == before


async def test_the_database_clock_is_used_without_a_test_clock(env) -> None:
    wid = await env.workspace()
    result = await env.charge(wid, 3)
    async with env.factory() as s:
        db_now = (await s.execute(text("SELECT timezone('UTC', statement_timestamp())"))).scalar()
    assert result.admitted
    assert abs(db_now - result.period_started_at) < timedelta(minutes=1)
    stored = await env.row(wid)
    assert stored.updated_at == stored.period_started_at


# ----------------------------------------------------------------------------
# 10: lock timeout fails closed
# ----------------------------------------------------------------------------


def _lock_not_available(exc: BaseException) -> bool:
    """The charge failed on the transaction-local ``lock_timeout`` (SQLSTATE 55P03)."""
    cause = exc.__cause__
    if not isinstance(cause, DBAPIError):
        return False
    orig = cause.orig
    codes = {
        getattr(orig, "sqlstate", None),
        getattr(orig, "pgcode", None),
        getattr(getattr(orig, "__cause__", None), "sqlstate", None),
    }
    return "55P03" in codes


async def test_lock_timeout_fails_closed_and_changes_nothing(env, monkeypatch) -> None:
    # Only the transaction-local lock timeout can end this wait: the overall
    # bound is far away.
    monkeypatch.setattr(pub, "CHARGE_LOCK_TIMEOUT_MS", 200)
    monkeypatch.setattr(pub, "CHARGE_TIMEOUT_SECONDS", 60.0)
    wid = await env.workspace()
    await env.store_row(wid, used=1, start=T0)
    before = await env.row(wid)
    async with env.factory() as holder:
        await holder.execute(
            text("SELECT 1 FROM public_usage_budgets WHERE workspace_id = :ws FOR UPDATE"),
            {"ws": wid},
        )
        started = time.monotonic()
        with pytest.raises(PublicUsageBudgetUnavailable) as excinfo:
            await env.charge(wid, 10, now=T0 + timedelta(hours=1))
        elapsed = time.monotonic() - started
        await holder.rollback()
    assert elapsed < 1.5
    assert _lock_not_available(excinfo.value), repr(excinfo.value.__cause__)
    assert await env.row(wid) == before


async def test_overall_bound_fails_closed(env, monkeypatch) -> None:
    # Only the overall bound can end this wait: the lock timeout is far away.
    monkeypatch.setattr(pub, "CHARGE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(pub, "CHARGE_LOCK_TIMEOUT_MS", 60_000)
    wid = await env.workspace()
    await env.store_row(wid, used=1, start=T0)
    async with env.factory() as holder:
        await holder.execute(
            text("SELECT 1 FROM public_usage_budgets WHERE workspace_id = :ws FOR UPDATE"),
            {"ws": wid},
        )
        started = time.monotonic()
        with pytest.raises(PublicUsageBudgetUnavailable) as excinfo:
            await env.charge(wid, 10, now=T0 + timedelta(hours=1))
        elapsed = time.monotonic() - started
        await holder.rollback()
    assert elapsed < 1.5
    assert isinstance(excinfo.value.__cause__, TimeoutError)
    assert (await env.row(wid)).calls_used == 1


async def test_a_locked_workspace_row_blocks_only_a_first_charge(env, monkeypatch) -> None:
    monkeypatch.setattr(pub, "CHARGE_LOCK_TIMEOUT_MS", 300)
    monkeypatch.setattr(pub, "CHARGE_TIMEOUT_SECONDS", 60.0)
    stored, fresh = await env.workspace(), await env.workspace()
    await env.store_row(stored, used=1, start=T0)
    async with env.factory() as holder:
        await holder.execute(
            text("SELECT 1 FROM workspaces WHERE id IN (:a, :b) FOR UPDATE"),
            {"a": stored, "b": fresh},
        )
        result = await env.charge(stored, 10, now=T0 + timedelta(hours=1))
        assert result.admitted and result.calls_used == 2
        started = time.monotonic()
        with pytest.raises(PublicUsageBudgetUnavailable) as excinfo:
            await env.charge(fresh, 10, now=T0)
        elapsed = time.monotonic() - started
        await holder.rollback()
    assert elapsed < 1.5
    assert _lock_not_available(excinfo.value), repr(excinfo.value.__cause__)
    assert await env.row(fresh) is None


async def test_the_charge_never_touches_the_callers_transaction(env) -> None:
    """The charge commits on its own session: the caller's stays open and untouched."""
    wid = await env.workspace()
    pending = uuid4()
    try:
        async with env.factory() as s:
            s.add(Workspace(id=pending, name=f"pending-{pending.hex[:8]}", owner_user_id="p"))
            await s.flush()
            assert s.in_transaction()
            for admitted, used in ((True, 1), (True, 2), (False, 2)):
                result = await charge_public_usage(wid, limit=2, db=s)
                assert result.admitted is admitted
                assert s.in_transaction()
                assert await s.scalar(select(Workspace.id).where(Workspace.id == pending))
                async with env.factory() as other:
                    assert await other.get(Workspace, pending) is None
                assert (await env.row(wid)).calls_used == used
            await s.rollback()
    finally:
        async with env.factory() as s:
            await s.execute(delete(Workspace).where(Workspace.id == pending))
            await s.commit()


# ----------------------------------------------------------------------------
# 11: identity survives installation, entitlement, owner and soft-delete changes
# ----------------------------------------------------------------------------


async def test_period_identity_survives_installation_changes(store, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_enabled", True)
    bind = SimpleNamespace(bind=None)
    async with store.factory() as s:
        bind.bind = s.bind
    approval = await approve(store)
    await paid(store)
    result = (await http(store, "POST", "", billing=True, body=activation(approval))).json()
    for _ in range(3):
        assert (await charge_public_usage(store.wid, limit=50, db=bind)).admitted

    async def snapshot():
        async with store.factory() as s:
            return (
                await s.execute(
                    text(
                        "SELECT period_started_at, calls_used, period_seq "
                        "FROM public_usage_budgets WHERE workspace_id = :ws"
                    ),
                    {"ws": store.wid},
                )
            ).one_or_none()

    before = await snapshot()
    assert before is not None and before.calls_used == 3

    await paid(store, 2, services=False)  # entitlement snapshot with services: []
    assert await snapshot() == before
    await transfer(store, "next")
    assert await snapshot() == before
    unlinked = await http(
        store, "DELETE", "sync-installations/" + result["installation_id"], role="next"
    )
    assert unlinked.status_code == 200, unlinked.text
    assert await snapshot() == before
    await change(store, Workspace, store.wid, deleted_at=store.clock.now)
    assert await snapshot() == before
    await change(store, Workspace, store.wid, deleted_at=None)
    assert await snapshot() == before

    async with store.factory() as s:
        await s.execute(delete(Workspace).where(Workspace.id == store.wid))
        await s.commit()
    assert await snapshot() is None


# ----------------------------------------------------------------------------
# 12-14: the read, the refill, a lowered limit
# ----------------------------------------------------------------------------


async def test_the_read_never_writes(env, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_enabled", True)
    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_calls", 5)
    empty, expired, running = await env.workspace(), await env.workspace(), await env.workspace()
    long_ago = datetime(2020, 1, 1)
    await env.store_row(expired, used=5, start=long_ago, seq=2)
    async with env.factory() as s:
        db_now = (await s.execute(text("SELECT timezone('UTC', statement_timestamp())"))).scalar()
    await env.store_row(running, used=5, start=db_now - timedelta(days=1), seq=7)
    before = (await env.row(expired), await env.row(running))

    async with env.factory() as s:
        idle = await read_public_usage_budget(s, empty)
        rolled = await read_public_usage_budget(s, expired)
        exhausted = await read_public_usage_budget(s, running)
        assert not s.in_transaction() or not s.dirty
        await s.rollback()

    assert idle.state == "idle" and idle.used == 0 and idle.remaining == 5
    assert rolled.state == "idle" and rolled.period_seq is None
    assert exhausted.state == "exhausted"
    assert (exhausted.used, exhausted.remaining, exhausted.period_seq) == (5, 0, 7)
    assert exhausted.resets_at == exhausted.period_started_at + WEEK
    assert abs(exhausted.as_of - db_now) < timedelta(minutes=1)
    assert await env.row(empty) is None
    assert (await env.row(expired), await env.row(running)) == before

    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_enabled", False)
    async with env.factory() as s:
        disabled = await read_public_usage_budget(s, running)
    assert disabled.state == "disabled" and disabled.used is None


async def test_reset_refills_and_keeps_the_period(env) -> None:
    wid, missing = await env.workspace(), await env.workspace()
    await env.store_row(wid, used=9, start=T0, seq=4)
    before = await env.row(wid)
    async with env.factory() as s:
        plan = await reset_public_usage_budget(s, wid, dry_run=True)
    assert (plan.calls_used_before, plan.period_seq, plan.applied) == (9, 4, False)
    assert await env.row(wid) == before
    with patch.object(pub, "logger") as log:
        async with env.factory() as s:
            applied = await reset_public_usage_budget(s, wid, dry_run=False)
    assert (applied.calls_used_before, applied.period_seq, applied.applied) == (9, 4, True)
    log.warning.assert_called_once_with(
        "public_usage_budget_reset", workspace_id=str(wid), period_seq=4, calls_used_before=9
    )
    after = await env.row(wid)
    assert (after.calls_used, after.period_started_at, after.period_seq) == (0, T0, 4)
    assert after.updated_at != before.updated_at
    for dry_run in (True, False):
        async with env.factory() as s:
            with pytest.raises(LookupError):
                await reset_public_usage_budget(s, missing, dry_run=dry_run)
    assert await env.row(missing) is None


async def test_a_lowered_limit_refuses_until_rollover(env) -> None:
    wid = await env.workspace()
    await env.store_row(wid, used=5, start=T0)
    assert not (await env.charge(wid, 3, now=T0 + timedelta(days=2))).admitted
    assert (await env.row(wid)).calls_used == 5
    rolled = await env.charge(wid, 3, now=T0 + WEEK)
    assert rolled.admitted and rolled.calls_used == 1


# ----------------------------------------------------------------------------
# MCP and the anonymous REST search spend the same row
# ----------------------------------------------------------------------------


async def _publish(env, wid: UUID, *, revoked: bool = False) -> Context:
    """A public resource context of ``wid`` with one public MCP endpoint row."""
    slug = "catalog-" + uuid4().hex[:6]
    async with env.factory() as s:
        resource = Resource(workspace_id=wid, resource_id=slug, created_by="owner")
        context = Context(
            id=uuid4(),
            workspace_id=wid,
            name=slug,
            created_by="owner",
            is_private=False,
            is_public=True,
            resource_id=slug,
        )
        s.add_all([resource, context])
        await s.flush()
        s.add(
            PublicMcpEndpoint(
                workspace_id=wid,
                context_id=context.id,
                resource_pk=resource.id,
                resource_id=slug,
                public_name="Catalog",
                created_by="owner",
                revoked_at=datetime(2030, 1, 1) if revoked else None,
                revoked_by="owner" if revoked else None,
            )
        )
        await s.commit()
    return context


async def test_mcp_and_rest_search_spend_the_same_row(env, dispatch_on, monkeypatch) -> None:
    from api.routes import public_search

    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_calls", 3)
    wid = await env.workspace()
    published = await _publish(env, wid)
    # Its only publication was revoked: no live publication, not counted.
    revoked = await _publish(env, wid, revoked=True)
    async with env.factory() as s:
        unpublished = Context(
            id=uuid4(),
            workspace_id=wid,
            name=published.name + "-old",
            created_by="owner",
            is_private=False,
            is_public=True,
        )
        s.add(unpublished)
        await s.commit()

    assert await dispatch_on(_serving(wid)) == "success"
    async with env.factory() as s:
        assert await public_search._has_live_publication(s, published.id) is True
        assert await public_search._has_live_publication(s, unpublished.id) is False
        assert await public_search._has_live_publication(s, revoked.id) is False
        await public_search._charge_public_usage_budget(s, published, bucket_counted=True)
        # Not published, or the publication was revoked: not counted, and an
        # unreadable bucket does not refuse it either.
        for context in (unpublished, revoked):
            assert (
                await public_search._charge_public_usage_budget(s, context, bucket_counted=False)
                is None
            )
    assert (await env.row(wid)).calls_used == 2
    assert await dispatch_on(_serving(wid)) == "success"
    async with env.factory() as s:
        from utils.exceptions import PublicUsageBudgetExhaustedError

        with pytest.raises(PublicUsageBudgetExhaustedError):
            await public_search._charge_public_usage_budget(s, published, bucket_counted=True)
    assert await dispatch_on(_serving(wid)) == "usage_budget_exhausted"
    assert (await env.row(wid)).calls_used == 3
    async with env.factory() as s:
        unused = await s.scalar(select(Workspace.id).where(Workspace.id == wid))
    assert unused == wid


async def test_concurrent_rest_charges_never_hold_two_connections(env, monkeypatch) -> None:
    """More concurrent charged searches than the pool can hold twice still pass.

    Each request holds its session's connection (the context read) when it
    reaches the charge. If it kept it while the charge checks out a second
    one, two requests on a pool of two would wait on each other until the
    charge bound refused both (hold-and-wait).
    """
    from api.routes import public_search

    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_enabled", True)
    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_calls", 10)
    monkeypatch.setattr(pub, "CHARGE_TIMEOUT_SECONDS", 2.0)
    wid = await env.workspace()
    published = await _publish(env, wid)
    small = create_async_engine(env.engine.url, pool_size=2, max_overflow=0, pool_timeout=30)
    factory = async_sessionmaker(small, class_=AsyncSession, expire_on_commit=False)
    holding = asyncio.Barrier(2)

    async def search() -> None:
        async with factory() as s:
            context = await s.get(Context, published.id)
            assert context is not None
            await holding.wait()  # both requests hold a connection now
            await public_search._charge_public_usage_budget(s, context, bucket_counted=True)
            # The search checks a connection out again and still sees the context.
            assert (await s.execute(text("SELECT 1"))).scalar_one() == 1
            assert context.workspace_id == wid
            await s.commit()

    try:
        await asyncio.gather(search(), search())
    finally:
        await small.dispose()
    assert (await env.row(wid)).calls_used == 2


# ----------------------------------------------------------------------------
# A failed handler stays counted (no refund, whatever its form)
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        lambda: RuntimeError("provider down"),
        lambda: TimeoutError(),
        lambda: PublicArgumentError("bad value"),
    ],
    ids=["error", "timeout", "argument"],
)
async def test_a_failed_handler_stays_counted_in_the_row(
    env, dispatch_on, monkeypatch, failure
) -> None:
    from mcp_server.public import dispatch
    from mcp_server.public.definitions import PUBLIC_TOOLS

    async def failing(endpoint, arguments):
        raise failure()

    monkeypatch.setattr(dispatch, "PUBLIC_HANDLERS", dict.fromkeys(PUBLIC_TOOLS, failing))
    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_calls", 10)
    wid = await env.workspace()
    endpoint = _serving(wid)
    for n in (1, 2, 3):
        assert await dispatch_on(endpoint) != "success"
        # Read after call_public_tool returned: nothing gave the unit back.
        assert (await env.row(wid)).calls_used == n
