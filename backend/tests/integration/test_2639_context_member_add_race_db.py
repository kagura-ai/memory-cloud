"""#2639: two concurrent adds of one user to a context leave one row.

``add_context_member`` checks for an existing ``context_members`` row and
then inserts. Two adds of the same user that both pass that check before
either commits used to insert two rows; ``uq_context_members_context_user``
now refuses the second insert at commit, and the route answers it with the
pre-check's 400 "already a context member".

The two calls run on sessions of their own against the test engine. Each
session's ``commit`` waits on a shared barrier, so both calls have passed the
pre-check before either writes: the overlap is forced, not left to timing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from mcp_server.tools._capacity_gate import invalidate_capacity_candidate_cache
from models.auth import Context, ContextMember, User, Workspace, WorkspaceMember

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Generous: the barrier releases as soon as both calls reach their commit. A
# call that never gets there fails the test instead of hanging it.
BARRIER_TIMEOUT_S = 15


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    owner: str
    target: str
    ws: UUID
    ctx: UUID


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    invalidate_capacity_candidate_cache()
    tag = uuid4().hex[:8]
    owner, target = f"cmr-own-{tag}", f"cmr-tgt-{tag}"
    async with factory() as s:
        for u in (owner, target):
            s.add(User(email=f"{u}@example.test", user_id=u, name=u, role="user"))
        await s.flush()
        ws = Workspace(
            id=uuid4(),
            name=f"cmr-{tag}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=50000,
            weekly_api_limit=250000,
        )
        s.add(ws)
        await s.flush()
        ctx = Context(
            id=uuid4(), workspace_id=ws.id, name=f"cmr-{tag}", created_by=owner, is_private=False
        )
        s.add(ctx)
        await s.flush()
        s.add_all(
            [
                WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER),
                # The list reaches the context: a plain add, no access-list extend.
                WorkspaceMember(
                    workspace_id=ws.id,
                    user_id=target,
                    role=WorkspaceRole.MEMBER,
                    allowed_context_ids=[ctx.id],
                ),
            ]
        )
        await s.commit()
        world = World(factory=factory, owner=owner, target=target, ws=ws.id, ctx=ctx.id)
    try:
        yield world
    finally:
        async with factory() as s:
            for stmt in (
                delete(Workspace).where(Workspace.id == world.ws),
                delete(User).where(User.user_id.in_([owner, target])),
            ):
                await s.execute(stmt)
                await s.commit()
        invalidate_capacity_candidate_cache()


async def _rows(w: World) -> int:
    async with w.factory() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(ContextMember)
                .where(ContextMember.context_id == w.ctx, ContextMember.user_id == w.target)
            )
        ).scalar_one()


async def test_concurrent_adds_of_one_user_leave_one_row_and_one_400(world: World) -> None:
    from api.routes.contexts import AddContextMemberRequest, add_context_member

    w = world
    principal: dict[str, Any] = {
        "user_id": w.owner,
        "sub": w.owner,
        "email": f"{w.owner}@example.test",
        "current_workspace_id": w.ws,
    }
    barrier = asyncio.Barrier(2)
    commits_reached = 0

    async def _add() -> Any:
        nonlocal commits_reached
        async with w.factory() as s:
            commit = s.commit

            async def _aligned_commit() -> None:
                # Both calls are past the existing-row check here; neither
                # has written its row yet.
                nonlocal commits_reached
                commits_reached += 1
                await asyncio.wait_for(barrier.wait(), timeout=BARRIER_TIMEOUT_S)
                await commit()

            with patch.object(s, "commit", _aligned_commit):
                return await add_context_member(
                    w.ctx,
                    AddContextMemberRequest(user_id=w.target, role="editor"),
                    MagicMock(),
                    s,
                )

    with patch("api.routes.contexts.get_current_user", AsyncMock(return_value=principal)):
        results = await asyncio.gather(_add(), _add(), return_exceptions=True)

    assert commits_reached == 2, results
    refused = [r for r in results if isinstance(r, HTTPException)]
    added = [r for r in results if not isinstance(r, BaseException)]
    assert len(added) == 1 and len(refused) == 1, results
    assert added[0].user_id == w.target
    assert refused[0].status_code == 400
    assert refused[0].detail == f"User {w.target} is already a context member"
    assert await _rows(w) == 1

    # A later add is refused by the existing-row check, as before.
    async with w.factory() as s:
        with (
            patch("api.routes.contexts.get_current_user", AsyncMock(return_value=principal)),
            pytest.raises(HTTPException) as again,
        ):
            await add_context_member(
                w.ctx, AddContextMemberRequest(user_id=w.target, role="viewer"), MagicMock(), s
            )
    assert again.value.status_code == 400
    assert again.value.detail == refused[0].detail
    assert await _rows(w) == 1
