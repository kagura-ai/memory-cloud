"""#1943: seats and writable slots when a plan drops below what a workspace holds.

Nothing is stored (same stance as #1939 / #1941): at use time the workspace's
members are ranked — owner first, then earliest ``COALESCE(joined_at,
invited_at)``, then ``id`` — and the first ``effective_max_members`` keep their
seat; its live contexts are ranked by ``created_at, id`` and the first
``effective_max_contexts`` stay writable. A member without a seat is refused
(``FEAT-001`` ``members``); a context without a slot refuses content writes
(``FEAT-001`` ``contexts``) while reads, listing, export and delete stay open.
Re-subscribing (or removing members / contexts) restores everything on the
next call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from config.constants import GATE_PLAN
from models.auth import Context, User, Workspace, WorkspaceMember
from services.plan_suspension import (
    SUSPENDED_CONTEXTS,
    SUSPENDED_MEMBERS,
    context_writes_suspended,
    ensure_context_writable,
    ensure_member_seated,
    member_seat_suspended,
    seat_paused_member_ids,
    suspended_features,
    writes_paused_context_ids,
)
from utils.datetime import utcnow
from utils.exceptions import FeatureNotAvailableError

T0 = utcnow() - timedelta(days=30)


async def _user(db: AsyncSession, tag: str) -> str:
    user_id = f"u_{tag}_{uuid4().hex[:6]}"
    db.add(
        User(
            email=f"{user_id}@seats.invalid",
            user_id=user_id,
            name=tag,
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    return user_id


async def _workspace(db: AsyncSession, plan: str, owner: str, **addons: int) -> Workspace:
    ws = Workspace(
        id=uuid4(),
        name=f"seats-{uuid4().hex[:8]}",
        plan_name=plan,
        owner_user_id=owner,
        daily_api_limit=500,
        weekly_api_limit=2500,
        **addons,
    )
    db.add(ws)
    await db.flush()
    db.add(
        WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER, joined_at=T0)
    )
    await db.flush()
    return ws


async def _member(db, ws, user_id, *, role=WorkspaceRole.MEMBER, joined_at=None, invited_at=None):
    m = WorkspaceMember(
        workspace_id=ws.id, user_id=user_id, role=role, joined_at=joined_at, invited_at=invited_at
    )
    db.add(m)
    await db.flush()
    return m


async def _context(db, ws, creator, name, created_at):
    c = Context(
        id=uuid4(), workspace_id=ws.id, name=name, created_by=creator, created_at=created_at
    )
    db.add(c)
    await db.flush()
    return c


@pytest_asyncio.fixture(loop_scope="session")
async def seats(db_session: AsyncSession) -> AsyncIterator[dict[str, object]]:
    """A Free workspace that was once Pro: owner + three members, three contexts."""
    owner = await _user(db_session, "owner")
    ws = await _workspace(db_session, "free", owner)
    second = await _user(db_session, "second")
    third = await _user(db_session, "third")
    legacy = await _user(db_session, "legacy")
    m_second = await _member(db_session, ws, second, joined_at=T0 + timedelta(days=1))
    m_third = await _member(db_session, ws, third, joined_at=T0 + timedelta(days=2))
    # A legacy row: never got joined_at, only invited_at (ranks by it).
    m_legacy = await _member(db_session, ws, legacy, invited_at=T0 + timedelta(hours=12))
    c_old = await _context(db_session, ws, owner, "oldest", T0)
    c_mid = await _context(db_session, ws, owner, "middle", T0 + timedelta(days=1))
    c_new = await _context(db_session, ws, owner, "newest", T0 + timedelta(days=2))
    yield {
        "ws": ws,
        "owner": owner,
        "m_owner": (
            await db_session.execute(
                __import__("sqlalchemy")
                .select(WorkspaceMember)
                .where(WorkspaceMember.workspace_id == ws.id, WorkspaceMember.user_id == owner)
            )
        ).scalar_one(),
        "m_second": m_second,
        "m_third": m_third,
        "m_legacy": m_legacy,
        "c_old": c_old,
        "c_mid": c_mid,
        "c_new": c_new,
    }


def _refresh(ws: Workspace, plan: str, **addons: int) -> None:
    ws.plan_name = plan
    for k, v in addons.items():
        setattr(ws, k, v)
    ws.__dict__.pop("_plan_tier", None)  # one session; a request loads anew


@pytest.mark.asyncio(loop_scope="session")
class TestSeats:
    async def test_the_owner_always_has_a_seat(self, db_session, seats) -> None:
        assert not await member_seat_suspended(db_session, seats["ws"], seats["m_owner"])

    async def test_free_seats_only_the_owner(self, db_session, seats) -> None:
        for key in ("m_second", "m_third", "m_legacy"):
            assert await member_seat_suspended(db_session, seats["ws"], seats[key]), key

    async def test_seats_go_to_the_earliest_joined(self, db_session, seats) -> None:
        """Three seats: owner, then the legacy row (invited 12h after T0 — it
        ranks by invited_at when joined_at is NULL), then 'second'."""
        _refresh(seats["ws"], "free", addon_member_bonus=0)
        _refresh(seats["ws"], "pro")  # 10 seats: everyone sits
        for key in ("m_second", "m_third", "m_legacy"):
            assert not await member_seat_suspended(db_session, seats["ws"], seats[key]), key
        # Shrink to three seats via a Basic-like cap: use the addon on free? Free's
        # base is 1 and zero-floor keeps addons from raising a 0 base, so model
        # "three seats" with pro minus an explicit limit through the helper's
        # ranking: the fourth-ranked member is 'third'.
        paused = await seat_paused_member_ids(db_session, seats["ws"], limit=3)
        assert paused == {seats["m_third"].id}

    async def test_refusal_names_the_tier_that_seats_everyone(self, db_session, seats) -> None:
        with pytest.raises(FeatureNotAvailableError) as exc:
            await ensure_member_seated(db_session, seats["ws"], seats["m_second"])
        assert exc.value.status_code == 403
        assert exc.value.error_code == "FEAT-001"
        d = exc.value.details
        assert d["gate"] == GATE_PLAN
        assert d["feature"] == SUSPENDED_MEMBERS
        assert d["limit"] == 1 and d["current"] == 4
        assert d["required_plan"] == "pro"  # 4 members need a 10-seat tier

    async def test_the_owner_is_never_refused(self, db_session, seats) -> None:
        await ensure_member_seated(db_session, seats["ws"], seats["m_owner"])

    async def test_paused_member_ids_on_free(self, db_session, seats) -> None:
        paused = await seat_paused_member_ids(db_session, seats["ws"])
        assert paused == {seats["m_second"].id, seats["m_third"].id, seats["m_legacy"].id}


@pytest.mark.asyncio(loop_scope="session")
class TestContextSlots:
    async def test_free_keeps_the_oldest_context_writable(self, db_session, seats) -> None:
        assert not await context_writes_suspended(db_session, seats["ws"], seats["c_old"])
        assert await context_writes_suspended(db_session, seats["ws"], seats["c_mid"])
        assert await context_writes_suspended(db_session, seats["ws"], seats["c_new"])

    async def test_pro_has_room_for_all(self, db_session, seats) -> None:
        _refresh(seats["ws"], "pro")
        for key in ("c_old", "c_mid", "c_new"):
            assert not await context_writes_suspended(db_session, seats["ws"], seats[key]), key

    async def test_a_deleted_context_frees_its_slot(self, db_session, seats) -> None:
        seats["c_old"].deleted_at = utcnow()
        await db_session.flush()
        assert not await context_writes_suspended(db_session, seats["ws"], seats["c_mid"])
        assert await context_writes_suspended(db_session, seats["ws"], seats["c_new"])

    async def test_refusal_for_a_context_without_a_slot(self, db_session, seats) -> None:
        with pytest.raises(FeatureNotAvailableError) as exc:
            await ensure_context_writable(db_session, seats["c_new"])
        d = exc.value.details
        assert d["feature"] == SUSPENDED_CONTEXTS and d["gate"] == GATE_PLAN
        assert d["limit"] == 1 and d["current"] == 3
        assert d["required_plan"] == "basic"  # 3 contexts fit Basic's 3
        await ensure_context_writable(db_session, seats["c_old"])  # the slot holder passes

    async def test_paused_context_ids_on_free(self, db_session, seats) -> None:
        paused = await writes_paused_context_ids(db_session, seats["ws"])
        assert paused == {seats["c_mid"].id, seats["c_new"].id}


@pytest.mark.asyncio(loop_scope="session")
class TestSuspendedList:
    async def test_members_and_contexts_are_listed_only_when_over(self, db_session, seats) -> None:
        listed = await suspended_features(db_session, seats["ws"])
        assert SUSPENDED_MEMBERS in listed and SUSPENDED_CONTEXTS in listed
        assert listed.index(SUSPENDED_MEMBERS) < listed.index(SUSPENDED_CONTEXTS)
        _refresh(seats["ws"], "pro")
        listed = await suspended_features(db_session, seats["ws"])
        assert SUSPENDED_MEMBERS not in listed and SUSPENDED_CONTEXTS not in listed
