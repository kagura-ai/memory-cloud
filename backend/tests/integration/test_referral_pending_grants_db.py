"""Pending referral grants a busy workspace kept from being credited (#2563).

``apply_pending_grants`` takes the user's oldest live owned workspace row
``NOWAIT`` (#2443). When the tries lose, the grants stay pending; the
scheduler sweep (``tasks.referral_tasks``) and the referral summary are the
points that apply them later. The fixtures are the #2443 referral section's of
``test_erasure_lock_order_db``.
"""

# ruff: noqa: F811 — the referral_world fixture is shared with test_erasure_lock_order_db

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from config.settings import get_settings
from models.auth import Workspace
from models.referral import ReferralGrant
from services.referral_service import ReferralService
from services.workspace_service import WorkspaceService
from tasks.referral_tasks import sweep_pending_referral_grants
from utils.datetime import utcnow

from .test_erasure_lock_order_db import (  # noqa: F401 (fixtures)
    _assert_global_order,
    _bonus,
    _LockTrace,
    _referral,
    referral_world,
)

pytestmark = pytest.mark.timeout(90)


@contextmanager
def _no_backoff() -> Iterator[None]:
    """The ``NOWAIT`` tries run without the sleeps between them."""
    with patch("services.workspace_locks.asyncio", SimpleNamespace(sleep=AsyncMock())):
        yield


async def _referee_workspace(db, referee: str) -> UUID | None:
    return await db.scalar(
        select(ReferralGrant.referred_workspace_id).where(ReferralGrant.referred_user_id == referee)
    )


def _sessions(db_session) -> async_sessionmaker:
    return async_sessionmaker(db_session.bind, expire_on_commit=False)


def _assert_apply_order(trace: _LockTrace) -> None:
    """Every apply: no users row, the workspace row ``NOWAIT`` and before the
    grant rows (whose foreign-key check would otherwise wait on it)."""
    _assert_global_order(trace)
    applies = [tx for tx in trace.kinds() if ("child", False) in tx]
    assert applies
    for tx in applies:
        assert not {("users", False), ("users", True), ("users_dml", False)} & set(tx), tx
        assert ("ws_live", False) not in tx, tx
        assert tx.index(("ws_live", True)) < tx.index(("child", False)), tx


async def _pending_referee_with_a_workspace(world, db_session):
    """A referee referred while owning no workspace, who owns one now
    (created directly, so nothing applied the grant yet)."""
    referral = await _referral(world, referee_owns_a_workspace=False)
    await ReferralService(db_session).redeem(referred_user_id=referral.referee, code=referral.code)
    workspace_id = (await world.workspace(referral.referee)).id
    await db_session.commit()
    assert await _referee_workspace(db_session, referral.referee) is None
    return referral, workspace_id


@pytest.mark.asyncio
async def test_a_grant_whose_apply_lost_its_tries_is_credited_by_the_sweep(
    db_session, referral_world
):
    """The issue's scenario. Creating a second workspace applies the pending
    grant to the referee's oldest owned workspace, which another transaction
    holds: the ``NOWAIT`` tries lose, ``create_workspace`` swallows the
    ``ConflictError`` and the grant stays pending. Once the holder is done the
    sweep credits it, holding no users row and taking the workspace ``NOWAIT``."""
    world = referral_world
    referral, first = await _pending_referee_with_a_workspace(world, db_session)
    maker = _sessions(db_session)

    async with maker() as holder:
        await holder.execute(select(Workspace.id).where(Workspace.id == first).with_for_update())
        with _no_backoff(), structlog.testing.capture_logs() as logs:
            async with maker() as db:
                second = await asyncio.wait_for(
                    WorkspaceService(db).create_workspace(
                        name="second", owner_user_id=referral.referee
                    ),
                    15,
                )
        world.workspace_ids.append(second.id)
        await holder.commit()

    # the rollback of the swallowed failure did not hand back an expired object
    assert (second.name, second.owner_user_id) == ("second", referral.referee)
    assert [e for e in logs if e["event"] == "referral_pending_grants_apply_failed"]
    assert await _referee_workspace(db_session, referral.referee) is None
    assert await _bonus(db_session, first) == 0

    with _LockTrace(db_session.bind) as trace:
        async with maker() as db:
            result = await asyncio.wait_for(sweep_pending_referral_grants(db), 15)

    assert result.grants_applied >= 1 and result.busy == 0
    _assert_apply_order(trace)
    assert await _referee_workspace(db_session, referral.referee) == first
    assert await _bonus(db_session, first) == get_settings().referral_referee_reward_memories
    assert await _bonus(db_session, second.id) == 0


@pytest.mark.asyncio
async def test_a_sweep_that_finds_the_workspace_busy_leaves_the_grant_for_the_next_run(
    db_session, referral_world
):
    """A sweep loses its tries too: the user is counted busy, the grant stays
    pending with nothing of the apply committed, and the next run credits it."""
    world = referral_world
    referral, workspace_id = await _pending_referee_with_a_workspace(world, db_session)
    maker = _sessions(db_session)

    async with maker() as holder:
        await holder.execute(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
        )
        with _no_backoff():
            async with maker() as db:
                busy = await asyncio.wait_for(sweep_pending_referral_grants(db), 15)
        await holder.rollback()

    assert busy.busy >= 1
    assert await _referee_workspace(db_session, referral.referee) is None
    assert await _bonus(db_session, workspace_id) == 0

    async with maker() as db:
        await asyncio.wait_for(sweep_pending_referral_grants(db), 15)
    assert await _referee_workspace(db_session, referral.referee) == workspace_id
    assert await _bonus(db_session, workspace_id) == (
        get_settings().referral_referee_reward_memories
    )


@pytest.mark.asyncio
async def test_the_sweep_selects_pending_users_who_own_a_live_workspace_oldest_first(
    db_session, referral_world
):
    """Pending on either side: no workspace credited, or a soft-deleted one.
    A user who owns no live workspace has nothing to credit yet and a revoked
    grant is not pending, so neither is selected. The limit bounds a run;
    the oldest grant goes first and the next run takes the rest."""
    world = referral_world
    settings = get_settings()

    async def user_with_workspace(prefix: str) -> tuple[str, UUID]:
        user_id = await world.user(prefix)
        return user_id, (await world.workspace(user_id)).id

    referrer, referrer_ws = await user_with_workspace("referrer")
    null_side, null_side_ws = await user_with_workspace("nullside")  # 2000-01-01
    no_workspace = await world.user("noworkspace")  # 2000-01-02
    revoked, _ = await user_with_workspace("revoked")  # 2000-01-03
    dead_side, dead_side_ws = await user_with_workspace("deadside")  # 2000-01-04
    dead_ws = (await world.workspace(dead_side)).id
    await db_session.execute(
        update(Workspace).where(Workspace.id == dead_ws).values(deleted_at=utcnow())
    )
    referrer_side, referrer_side_ws = await user_with_workspace("referrerside")  # 2000-01-05
    unlisted = await world.user("unlisted")

    def grant(day: int, **sides) -> ReferralGrant:
        return ReferralGrant(
            referrer_bonus_memories=settings.referral_referrer_reward_memories,
            referred_bonus_memories=settings.referral_referee_reward_memories,
            granted_at=datetime(2000, 1, day),
            **sides,
        )

    credited = {"referrer_user_id": referrer, "referrer_workspace_id": referrer_ws}
    revoke = {"revoked_at": utcnow(), "revoked_reason": "test"}
    db_session.add_all(
        [
            grant(1, referred_user_id=null_side, **credited),
            grant(2, referred_user_id=no_workspace, **credited),
            grant(3, referred_user_id=revoked, **credited, **revoke),
            grant(4, referred_user_id=dead_side, referred_workspace_id=dead_ws, **credited),
            grant(5, referrer_user_id=referrer_side, referred_user_id=unlisted),
        ]
    )
    await db_session.commit()
    ours = set(world.targets)
    maker = _sessions(db_session)

    async with maker() as db:
        selected = await ReferralService(db).users_with_pending_grants(limit=1000)
    assert [u for u in selected if u in ours] == [null_side, dead_side, referrer_side]

    async with maker() as db:
        assert await ReferralService(db).users_with_pending_grants(limit=2) == [
            null_side,
            dead_side,
        ]

    async with maker() as db:
        first_run = await sweep_pending_referral_grants(db, limit=1)
    assert (first_run.candidates, first_run.users_applied) == (1, 1)
    assert await _referee_workspace(db_session, null_side) == null_side_ws
    assert await _referee_workspace(db_session, dead_side) == dead_ws  # the next run's

    async with maker() as db:
        await sweep_pending_referral_grants(db)
    assert await _referee_workspace(db_session, dead_side) == dead_side_ws
    assert (
        await db_session.scalar(
            select(ReferralGrant.referrer_workspace_id).where(
                ReferralGrant.referrer_user_id == referrer_side
            )
        )
        == referrer_side_ws
    )
    assert await _bonus(db_session, null_side_ws) == settings.referral_referee_reward_memories
    assert await _bonus(db_session, dead_side_ws) == settings.referral_referee_reward_memories
    assert await _bonus(db_session, referrer_side_ws) == (
        settings.referral_referrer_reward_memories
    )
    # left alone: nothing to credit yet, and a revoked grant
    assert await _referee_workspace(db_session, no_workspace) is None
    assert await _referee_workspace(db_session, revoked) is None
    async with maker() as db:
        assert not {no_workspace, revoked} & set(
            await ReferralService(db).users_with_pending_grants(limit=1000)
        )


@pytest.mark.asyncio
async def test_an_apply_that_finds_nothing_to_rehome_lets_go_of_the_workspace(
    db_session, referral_world
):
    """The grants were applied between the pending check and the UPDATEs
    (simulated: the check says yes, nothing is pending). The apply returns 0
    and does not leave the workspace row locked in the caller's transaction."""
    world = referral_world
    user_id = await world.user("nothingpending")
    workspace_id = (await world.workspace(user_id)).id
    await db_session.commit()
    maker = _sessions(db_session)

    async with maker() as db:
        with patch.object(ReferralService, "_has_pending_grants", AsyncMock(return_value=True)):
            assert await ReferralService(db).apply_pending_grants(user_id) == 0
        async with maker() as other:
            locked = await other.scalar(
                select(Workspace.id)
                .where(Workspace.id == workspace_id)
                .with_for_update(nowait=True)
            )
            await other.rollback()
    assert locked == workspace_id


@pytest.mark.asyncio
async def test_reading_the_summary_applies_pending_grants(db_session, referral_world):
    referral, workspace_id = await _pending_referee_with_a_workspace(referral_world, db_session)

    with _LockTrace(db_session.bind) as trace:
        async with _sessions(db_session)() as db:
            summary = await ReferralService(db).get_summary(referral.referee)

    assert summary.code
    _assert_apply_order(trace)
    assert await _referee_workspace(db_session, referral.referee) == workspace_id
    assert await _bonus(db_session, workspace_id) == (
        get_settings().referral_referee_reward_memories
    )


@pytest.mark.asyncio
async def test_the_summary_renders_while_the_workspace_is_busy(db_session, referral_world):
    """The apply loses its tries: the ``ConflictError`` is swallowed and the
    summary renders. Its own commit (the code it mints) carries nothing of the
    apply, so the grant stays pending for the sweep."""
    referral, workspace_id = await _pending_referee_with_a_workspace(referral_world, db_session)
    maker = _sessions(db_session)

    async with maker() as holder:
        await holder.execute(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
        )
        with (
            _no_backoff(),
            structlog.testing.capture_logs() as logs,
            _LockTrace(db_session.bind) as trace,
        ):
            async with maker() as db:
                summary = await asyncio.wait_for(
                    ReferralService(db).get_summary(referral.referee), 15
                )
        await holder.rollback()

    assert summary.code
    _assert_global_order(trace)
    busy = next(e for e in logs if e["event"] == "referral_pending_grants_busy")
    assert busy["user_id"] == referral.referee
    assert await _referee_workspace(db_session, referral.referee) is None
    assert await _bonus(db_session, workspace_id) == 0
