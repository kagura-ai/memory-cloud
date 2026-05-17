"""Regression tests for #699 — admin user detail accessible_contexts role display.

Pins the role-derivation logic in ``admin.py:get_user_detail`` so workspace
``admin`` is no longer collapsed to context role ``owner``.

Hits a real Postgres test DB because the existing mock-DB tests in
``tests/api/test_admin_*.py`` cannot exercise the
``PermissionService.get_accessible_contexts`` + ``ContextMember`` batch
lookup the handler performs.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin import get_user_detail
from models.auth import Context, ContextMember, User, Workspace, WorkspaceMember


def _mock_admin() -> dict:
    return {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"}


def _new_user(role: str = "user") -> User:
    user_id = f"u_{uuid4().hex[:8]}"
    return User(
        email=f"{user_id}@test.invalid",
        user_id=user_id,
        name="Test User",
        role=role,
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
    )


def _new_workspace(owner_user_id: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_user_id,
        memory_limit=1000,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )


def _new_context(workspace_id, created_by: str) -> Context:
    return Context(
        workspace_id=workspace_id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=created_by,
        is_private=False,
    )


@pytest_asyncio.fixture
async def workspace_owner_no_ctx_member(db_session: AsyncSession) -> dict:
    """User is the workspace owner with no ContextMember row (full access via workspace role)."""
    user = _new_user()
    db_session.add(user)
    await db_session.flush()

    ws = _new_workspace(owner_user_id=user.user_id)
    db_session.add(ws)
    await db_session.flush()

    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.user_id, role="owner"))

    ctx = _new_context(workspace_id=ws.id, created_by=user.user_id)
    db_session.add(ctx)
    await db_session.commit()

    return {"user_id": user.user_id, "context_id": str(ctx.id)}


@pytest_asyncio.fixture
async def workspace_admin_no_ctx_member(db_session: AsyncSession) -> dict:
    """User is workspace admin (NOT owner) with NO ContextMember row.

    Regression pin for #699: previously displayed as ctx_role='owner'.
    """
    owner_user = _new_user()
    admin_user = _new_user()
    db_session.add_all([owner_user, admin_user])
    await db_session.flush()

    ws = _new_workspace(owner_user_id=owner_user.user_id)
    db_session.add(ws)
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=owner_user.user_id, role="owner"),
            WorkspaceMember(workspace_id=ws.id, user_id=admin_user.user_id, role="admin"),
        ]
    )

    ctx = _new_context(workspace_id=ws.id, created_by=owner_user.user_id)
    db_session.add(ctx)
    await db_session.commit()
    # NOTE: no ContextMember row for admin_user — that's the point of this fixture.

    return {"user_id": admin_user.user_id, "context_id": str(ctx.id)}


@pytest_asyncio.fixture
async def workspace_member_with_ctx_member(db_session: AsyncSession) -> dict:
    """User is workspace member (allowed_context_ids whitelisted) with an explicit
    ContextMember(role='editor') row.
    """
    owner_user = _new_user()
    member_user = _new_user()
    db_session.add_all([owner_user, member_user])
    await db_session.flush()

    ws = _new_workspace(owner_user_id=owner_user.user_id)
    db_session.add(ws)
    await db_session.flush()

    ctx = _new_context(workspace_id=ws.id, created_by=owner_user.user_id)
    db_session.add(ctx)
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=owner_user.user_id, role="owner"),
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=member_user.user_id,
                role="member",
                allowed_context_ids=[ctx.id],
            ),
            ContextMember(context_id=ctx.id, user_id=member_user.user_id, role="editor"),
        ]
    )
    await db_session.commit()

    return {"user_id": member_user.user_id, "context_id": str(ctx.id)}


def _pick_context(detail, context_id: str):
    return next((c for c in detail.accessible_contexts if c.context_id == context_id), None)


class TestAccessibleContextRole:
    """#699 — ``UserAccessibleContext.role`` must reflect the user's actual access role,
    not collapse workspace admin to 'owner'."""

    @pytest.mark.asyncio
    async def test_workspace_owner_displays_owner(
        self,
        db_session: AsyncSession,
        workspace_owner_no_ctx_member: dict,
    ) -> None:
        detail = await get_user_detail(
            user_id=workspace_owner_no_ctx_member["user_id"],
            admin=_mock_admin(),
            db=db_session,
        )
        ctx = _pick_context(detail, workspace_owner_no_ctx_member["context_id"])
        assert ctx is not None, "test context must appear in accessible_contexts"
        assert ctx.role == "owner"

    @pytest.mark.asyncio
    async def test_workspace_admin_no_ctx_member_displays_admin(
        self,
        db_session: AsyncSession,
        workspace_admin_no_ctx_member: dict,
    ) -> None:
        detail = await get_user_detail(
            user_id=workspace_admin_no_ctx_member["user_id"],
            admin=_mock_admin(),
            db=db_session,
        )
        ctx = _pick_context(detail, workspace_admin_no_ctx_member["context_id"])
        assert ctx is not None, "test context must appear in accessible_contexts"
        assert ctx.role == "admin", (
            "workspace admin with no ContextMember row must display 'admin', "
            "not 'owner' (#699 regression pin)"
        )

    @pytest.mark.asyncio
    async def test_workspace_member_with_ctx_member_displays_explicit_role(
        self,
        db_session: AsyncSession,
        workspace_member_with_ctx_member: dict,
    ) -> None:
        detail = await get_user_detail(
            user_id=workspace_member_with_ctx_member["user_id"],
            admin=_mock_admin(),
            db=db_session,
        )
        ctx = _pick_context(detail, workspace_member_with_ctx_member["context_id"])
        assert ctx is not None, "test context must appear in accessible_contexts"
        assert ctx.role == "editor", (
            "workspace member with explicit ContextMember(role='editor') must "
            "display the ContextMember role"
        )
