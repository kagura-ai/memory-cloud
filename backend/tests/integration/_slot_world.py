"""Shared fixtures of the workspace slot DB tests (#2918).

Everything is committed through sessions of its own, so a second session can
see the rows and contend for the owner lock; ``cleanup`` deletes what was
seeded. The mode is switched on the cached settings object, the way the code
reads it in production.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.settings import get_settings
from models.auth import User, Workspace, WorkspaceMember
from models.workspace_slots import WorkspaceSlotState, WorkspaceSlotSuspension
from services.workspace_slots import owner_slot_lock, owner_slot_lock_key

T0 = datetime(2031, 6, 1, 12, 0, 0)


@contextmanager
def slot_mode(mode: str) -> Iterator[None]:
    """Run with ``WORKSPACE_SLOT_SUSPENSION=mode`` on the cached settings."""
    settings = get_settings()
    saved = (settings.workspace_slot_suspension, settings.enforce_workspace_cap)
    object.__setattr__(settings, "workspace_slot_suspension", mode)
    object.__setattr__(settings, "enforce_workspace_cap", mode == "enforce" or saved[1])
    try:
        yield
    finally:
        object.__setattr__(settings, "workspace_slot_suspension", saved[0])
        object.__setattr__(settings, "enforce_workspace_cap", saved[1])


class StatementRecorder:
    """Every SQL statement the engine runs while active."""

    def __init__(self, engine) -> None:
        self.engine = engine.sync_engine
        self.statements: list[str] = []

    def _record(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.statements.append(" ".join(statement.split()))

    def __enter__(self) -> StatementRecorder:
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)

    def touching_slot_tables(self) -> list[str]:
        return [
            s
            for s in self.statements
            if "workspace_slot_states" in s
            or "workspace_slot_suspensions" in s
            or owner_slot_lock_key("") in s
        ]

    def advisory_locks(self) -> list[str]:
        return [s for s in self.statements if "advisory_xact_lock" in s]


class SlotWorld:
    """Users, owned workspaces and slot rows, committed."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self.maker = maker
        self.users: list[str] = []
        self.workspaces: list[UUID] = []
        self._tick = 0

    async def user(self, prefix: str = "slot", *, bonus: int = 0) -> str:
        user_id = f"{prefix}-{uuid4().hex[:10]}"
        async with self.maker() as s:
            s.add(
                User(
                    user_id=user_id,
                    email=f"{user_id}@example.com",
                    name=prefix,
                    role="user",
                    workspace_slot_bonus=bonus,
                )
            )
            await s.commit()
        self.users.append(user_id)
        return user_id

    async def workspace(
        self,
        owner: str,
        *,
        plan: str = "free",
        source: str = "admin_grant",
        current: bool = False,
        members: dict[str, str] | None = None,
    ) -> UUID:
        self._tick += 1
        wid = uuid4()
        async with self.maker() as s:
            s.add(
                Workspace(
                    id=wid,
                    name=f"slots-{wid.hex[:8]}",
                    owner_user_id=owner,
                    plan_name=plan,
                    entitlement_source=source,
                    created_at=T0 + timedelta(days=self._tick),
                )
            )
            await s.flush()
            s.add(WorkspaceMember(workspace_id=wid, user_id=owner, role="owner"))
            for user_id, role in (members or {}).items():
                s.add(WorkspaceMember(workspace_id=wid, user_id=user_id, role=role))
            if current:
                await s.execute(
                    update(User).where(User.user_id == owner).values(current_workspace_id=wid)
                )
            await s.commit()
        self.workspaces.append(wid)
        return wid

    async def set_plan(self, wid: UUID, plan: str, source: str) -> None:
        async with self.maker() as s:
            await s.execute(
                update(Workspace)
                .where(Workspace.id == wid)
                .values(plan_name=plan, entitlement_source=source)
            )
            await s.commit()

    async def set_owner(self, wid: UUID, owner: str) -> None:
        async with self.maker() as s:
            await s.execute(
                update(Workspace).where(Workspace.id == wid).values(owner_user_id=owner)
            )
            await s.commit()

    async def soft_delete(self, wid: UUID) -> None:
        async with self.maker() as s:
            await s.execute(update(Workspace).where(Workspace.id == wid).values(deleted_at=T0))
            await s.commit()

    async def suspend_row(self, wid: UUID, owner: str, cause: str = "billing_snapshot") -> None:
        async with self.maker() as s:
            s.add(
                WorkspaceSlotSuspension(
                    workspace_id=wid, owner_user_id=owner, cause=cause, suspended_at=T0
                )
            )
            await s.commit()

    async def rows(self, owner: str | None = None) -> dict[UUID, str]:
        """``workspace_id -> owner_user_id`` of the suspension rows (of ``owner``)."""
        async with self.maker() as s:
            stmt = select(
                WorkspaceSlotSuspension.workspace_id, WorkspaceSlotSuspension.owner_user_id
            )
            if owner is not None:
                stmt = stmt.where(WorkspaceSlotSuspension.owner_user_id == owner)
            return {r.workspace_id: r.owner_user_id for r in (await s.execute(stmt)).all()}

    async def row_of(self, wid: UUID):
        async with self.maker() as s:
            return (
                await s.execute(
                    select(
                        WorkspaceSlotSuspension.owner_user_id,
                        WorkspaceSlotSuspension.cause,
                        WorkspaceSlotSuspension.reason,
                    ).where(WorkspaceSlotSuspension.workspace_id == wid)
                )
            ).one_or_none()

    async def state(self, owner: str):
        async with self.maker() as s:
            return (
                await s.execute(
                    select(
                        WorkspaceSlotState.keep_workspace_ids,
                        WorkspaceSlotState.selection_saved_at,
                        WorkspaceSlotState.selection_required_since,
                        WorkspaceSlotState.version,
                    ).where(WorkspaceSlotState.owner_user_id == owner)
                )
            ).one_or_none()

    async def plan_of(self, wid: UUID) -> tuple[str, str]:
        async with self.maker() as s:
            row = (
                await s.execute(
                    select(Workspace.plan_name, Workspace.entitlement_source).where(
                        Workspace.id == wid
                    )
                )
            ).one()
            return row.plan_name, row.entitlement_source

    async def owner_of(self, wid: UUID) -> str:
        async with self.maker() as s:
            return await s.scalar(select(Workspace.owner_user_id).where(Workspace.id == wid))

    async def hold_owner_lock(self, owner: str):
        """A second session holding ``owner``'s lock until ``release()``."""
        session = self.maker()
        assert await owner_slot_lock(session, owner, wait=True)
        released = asyncio.Event()

        async def release() -> None:
            if not released.is_set():
                released.set()
                await session.rollback()
                await session.close()

        return release

    async def cleanup(self) -> None:
        async with self.maker() as s:
            await s.execute(
                delete(WorkspaceSlotSuspension).where(
                    WorkspaceSlotSuspension.owner_user_id.in_(self.users)
                    | WorkspaceSlotSuspension.workspace_id.in_(self.workspaces)
                )
            )
            await s.execute(
                delete(WorkspaceSlotState).where(WorkspaceSlotState.owner_user_id.in_(self.users))
            )
            await s.execute(
                update(User).where(User.user_id.in_(self.users)).values(current_workspace_id=None)
            )
            await s.execute(
                delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(self.workspaces))
            )
            await s.execute(delete(Workspace).where(Workspace.id.in_(self.workspaces)))
            await s.execute(delete(User).where(User.user_id.in_(self.users)))
            await s.commit()
