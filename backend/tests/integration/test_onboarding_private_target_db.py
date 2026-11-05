"""The practice save's private-target check on real rows (#2633).

``MemoryService._check_private_target`` runs once unlocked during preparation
and once with ``lock=True`` right before the insert. On Postgres:

* only the caller's own private, non-public, live context in the workspace
  they own passes; a shared context, another user's private context or a
  different workspace is a 409 ``ConflictError``;
* the locked check holds the context row ``FOR SHARE`` and nothing else: a
  concurrent insert into the context (its foreign key takes ``FOR KEY SHARE``)
  is not held up, while a change to the row (``FOR NO KEY UPDATE``: sharing,
  publishing, soft-deleting it) waits until the practice save commits; the
  owner's membership row stays unlocked.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.auth import Context, Workspace, WorkspaceMember, WorkspaceRole
from services.memory_service import MemoryService
from utils.exceptions import ConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(async_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    owner = f"practice-it-{uuid.uuid4().hex[:8]}"
    other = f"practice-other-{uuid.uuid4().hex[:8]}"
    ws_id, other_ws = uuid.uuid4(), uuid.uuid4()
    ctx = {name: uuid.uuid4() for name in ("private", "shared", "theirs", "elsewhere")}
    async with _maker(async_engine)() as session:
        session.add_all(
            [
                Workspace(id=ws_id, name=f"ws-{owner}", owner_user_id=owner),
                Workspace(id=other_ws, name=f"ws2-{owner}", owner_user_id=owner),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(workspace_id=ws_id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=other_ws, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=ws_id, user_id=other, role=WorkspaceRole.MEMBER),
            ]
        )
        session.add_all(
            [
                Context(id=ctx["private"], workspace_id=ws_id, name="mine", created_by=owner),
                Context(
                    id=ctx["shared"],
                    workspace_id=ws_id,
                    name="shared",
                    created_by=owner,
                    is_private=False,
                ),
                Context(id=ctx["theirs"], workspace_id=ws_id, name="theirs", created_by=other),
                Context(id=ctx["elsewhere"], workspace_id=other_ws, name="mine", created_by=owner),
            ]
        )
        await session.commit()
    try:
        yield {"owner": owner, "workspace_id": ws_id, "other_workspace_id": other_ws, "ctx": ctx}
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(Workspace).where(Workspace.id.in_([ws_id, other_ws])))
            await session.commit()


async def _nowait(engine: AsyncEngine, sql: str, row_id: Any) -> bool:
    """Whether another transaction gets ``sql`` (a ``... NOWAIT`` lock) right now."""
    async with _maker(engine)() as probe:
        try:
            await probe.execute(text(sql), {"id": row_id})
        except DBAPIError as exc:
            assert getattr(exc.orig, "sqlstate", None) == "55P03"
            return False
        finally:
            await probe.rollback()
    return True


async def test_locked_check_shares_the_context_row_only(
    async_engine: AsyncEngine, seeded: dict[str, Any]
) -> None:
    owner, ws_id, ctx_id = seeded["owner"], seeded["workspace_id"], seeded["ctx"]["private"]
    async with _maker(async_engine)() as session:
        try:
            await MemoryService(session)._check_private_target(
                owner, ctx_id, ws_id, ws_id, lock=True
            )
            assert await _nowait(
                async_engine, "SELECT id FROM contexts WHERE id = :id FOR KEY SHARE NOWAIT", ctx_id
            )
            assert not await _nowait(
                async_engine,
                "SELECT id FROM contexts WHERE id = :id FOR NO KEY UPDATE NOWAIT",
                ctx_id,
            )
            member_id = (
                await session.execute(
                    text(
                        "SELECT id FROM workspace_members"
                        " WHERE workspace_id = :ws AND user_id = :user"
                    ),
                    {"ws": ws_id, "user": owner},
                )
            ).scalar_one()
            assert await _nowait(
                async_engine,
                "SELECT id FROM workspace_members WHERE id = :id FOR UPDATE NOWAIT",
                member_id,
            )
        finally:
            await session.rollback()


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize("target", ["shared", "theirs", "elsewhere"])
async def test_only_the_callers_own_private_context_passes(
    async_engine: AsyncEngine, seeded: dict[str, Any], target: str, lock: bool
) -> None:
    owner, ws_id = seeded["owner"], seeded["workspace_id"]
    async with _maker(async_engine)() as session:
        service = MemoryService(session)
        try:
            await service._check_private_target(
                owner, seeded["ctx"]["private"], ws_id, ws_id, lock=lock
            )
            with pytest.raises(ConflictError):
                await service._check_private_target(
                    owner, seeded["ctx"][target], ws_id, ws_id, lock=lock
                )
        finally:
            await session.rollback()
