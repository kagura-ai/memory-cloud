"""``seed_e2e_admin --slot-owner`` on real rows (#2918).

The seed writes, through ``seed_default_suspension``, what the authed a11y
spec of the workspace management page expects: one password user owning
three Free workspaces, the first current and active, the two others
suspended, the selection required, and one shared context without memories
in a suspended workspace. Running it twice changes nothing. The owner's
``GET /api/v1/me/workspace-slots`` body is checked through the route.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from api.main import app
from auth.dependencies import require_session_auth
from auth.password import hash_password
from cli.seed_e2e_admin import SLOT_OWNER_CONTEXT, SLOT_OWNER_WORKSPACES, seed_slot_owner
from db.base import get_db
from models.auth import Context, User, Workspace, WorkspaceMember
from models.workspace_slots import WorkspaceSlotState, WorkspaceSlotSuspension

from ._slot_world import SlotWorld, slot_mode

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_seeding_twice_leaves_one_owner_three_workspaces_two_suspended(
    slot_world: SlotWorld,
) -> None:
    login_id = f"e2e-slot-owner-{uuid4().hex[:8]}"
    password_hash = hash_password("Valid-passw0rd!")
    async with slot_world.maker() as s:
        first = await seed_slot_owner(s, login_id, password_hash)
    user_id = first.user_id
    slot_world.users.append(user_id)
    slot_world.workspaces.extend(first.workspace_ids)
    async with slot_world.maker() as s:
        second = await seed_slot_owner(s, login_id, password_hash)

    assert first.created is True
    assert second.created is False
    assert user_id == f"local:{login_id}"
    assert len(first.workspace_ids) == 3
    assert first.suspended == first.workspace_ids[1:]

    async with slot_world.maker() as s:
        users = (
            await s.execute(
                select(
                    User.role, User.auth_method, User.totp_enabled, User.current_workspace_id
                ).where(User.login_id == login_id)
            )
        ).all()
        workspaces = (
            await s.execute(
                select(Workspace.id, Workspace.name, Workspace.plan_name)
                .where(Workspace.owner_user_id == user_id)
                .order_by(Workspace.created_at)
            )
        ).all()
        memberships = await s.scalar(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.user_id == user_id, WorkspaceMember.role == "owner")
        )
        suspended = set(
            (
                await s.execute(
                    select(WorkspaceSlotSuspension.workspace_id).where(
                        WorkspaceSlotSuspension.owner_user_id == user_id
                    )
                )
            ).scalars()
        )
        state = (
            await s.execute(
                select(WorkspaceSlotState.selection_required_since).where(
                    WorkspaceSlotState.owner_user_id == user_id
                )
            )
        ).one()
        contexts = (
            await s.execute(
                select(Context.workspace_id, Context.name, Context.is_private).where(
                    Context.created_by == user_id
                )
            )
        ).all()

    assert users == [("user", "password", False, first.workspace_ids[0])]
    assert [(w.name, w.plan_name) for w in workspaces] == [
        (name, "free") for name in SLOT_OWNER_WORKSPACES
    ]
    assert memberships == 3
    assert suspended == set(first.workspace_ids[1:])
    assert state.selection_required_since is not None
    assert contexts == [(first.suspended[0], SLOT_OWNER_CONTEXT, False)]

    # What the page reads: "active 1 of 1", three owned, two suspended.
    async def _db():
        async with slot_world.maker() as session:
            yield session
            await session.commit()

    async def _session() -> dict:
        return {"user_id": user_id, "email": f"{login_id}@local"}

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_session_auth] = _session
    try:
        with slot_mode("enforce"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                body = (await client.get("/api/v1/me/workspace-slots")).json()
                listed = (
                    await client.get(
                        f"/api/v1/me/workspace-slots/workspaces/{first.suspended[0]}/contexts"
                    )
                ).json()
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_session_auth, None)

    assert (body["active_count"], body["active_slots"], body["owned_count"]) == (1, 1, 3)
    assert body["selection_required"] is True
    assert [row["name"] for row in body["workspaces"]] == list(SLOT_OWNER_WORKSPACES)
    assert [row["status"] for row in body["workspaces"]] == [
        "active",
        "slot_suspended",
        "slot_suspended",
    ]
    assert [row["is_current"] for row in body["workspaces"]] == [True, False, False]
    assert [c["name"] for c in listed["contexts"]] == [SLOT_OWNER_CONTEXT]
