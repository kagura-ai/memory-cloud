"""Regression tests for #699 — admin user detail accessible_contexts role display.

Pins the role-derivation logic in ``admin.py:get_user_detail`` so workspace
``admin`` is no longer collapsed to context role ``owner``.

Hits a real Postgres test DB because the existing mock-DB tests in
``tests/api/test_admin_*.py`` cannot exercise the
``PermissionService.get_accessible_contexts`` + ``ContextMember`` batch
lookup the handler performs.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin import get_user_detail
from auth.workspace_roles import ContextRole, WorkspaceRole
from models.auth import ContextMember, WorkspaceMember

from ._admin_helpers import make_context, make_user, make_workspace, mock_admin


@pytest_asyncio.fixture
async def workspace_owner_no_ctx_member(db_session: AsyncSession) -> dict:
    """User is the workspace owner with no ContextMember row (full access via workspace role)."""
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id)
    db_session.add(ws)
    await db_session.flush()

    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=user.user_id, role=WorkspaceRole.OWNER)
    )

    ctx = make_context(workspace_id=ws.id, created_by=user.user_id)
    db_session.add(ctx)
    await db_session.commit()

    return {"user_id": user.user_id, "context_id": str(ctx.id)}


@pytest_asyncio.fixture
async def workspace_admin_no_ctx_member(db_session: AsyncSession) -> dict:
    """User is workspace admin (NOT owner) with NO ContextMember row.

    Regression pin for #699: previously displayed as ctx_role='owner'.
    """
    owner_user = make_user()
    admin_user = make_user()
    db_session.add_all([owner_user, admin_user])
    await db_session.flush()

    ws = make_workspace(owner_user_id=owner_user.user_id)
    db_session.add(ws)
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=ws.id, user_id=owner_user.user_id, role=WorkspaceRole.OWNER
            ),
            WorkspaceMember(
                workspace_id=ws.id, user_id=admin_user.user_id, role=WorkspaceRole.ADMIN
            ),
        ]
    )

    ctx = make_context(workspace_id=ws.id, created_by=owner_user.user_id)
    db_session.add(ctx)
    await db_session.commit()
    # NOTE: no ContextMember row for admin_user — that's the point of this fixture.

    return {"user_id": admin_user.user_id, "context_id": str(ctx.id)}


@pytest_asyncio.fixture
async def workspace_member_no_ctx_member(db_session: AsyncSession) -> dict:
    """User is workspace member (allowed_context_ids whitelisted) with NO ContextMember row.

    Exercises the default fallback in admin.py:577 — when workspace_role is not in
    (owner, admin) and no ContextMember row exists, ctx_role stays at the "viewer" default.
    """
    owner_user = make_user()
    member_user = make_user()
    db_session.add_all([owner_user, member_user])
    await db_session.flush()

    ws = make_workspace(owner_user_id=owner_user.user_id)
    db_session.add(ws)
    await db_session.flush()

    ctx = make_context(workspace_id=ws.id, created_by=owner_user.user_id)
    db_session.add(ctx)
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=ws.id, user_id=owner_user.user_id, role=WorkspaceRole.OWNER
            ),
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=member_user.user_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[ctx.id],
            ),
        ]
    )
    await db_session.commit()
    # NOTE: no ContextMember row — exercises the "viewer" default fallback.

    return {"user_id": member_user.user_id, "context_id": str(ctx.id)}


@pytest_asyncio.fixture
async def workspace_member_with_ctx_member(db_session: AsyncSession) -> dict:
    """User is workspace member (allowed_context_ids whitelisted) with an explicit
    ContextMember(role='editor') row.
    """
    owner_user = make_user()
    member_user = make_user()
    db_session.add_all([owner_user, member_user])
    await db_session.flush()

    ws = make_workspace(owner_user_id=owner_user.user_id)
    db_session.add(ws)
    await db_session.flush()

    ctx = make_context(workspace_id=ws.id, created_by=owner_user.user_id)
    db_session.add(ctx)
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=ws.id, user_id=owner_user.user_id, role=WorkspaceRole.OWNER
            ),
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=member_user.user_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[ctx.id],
            ),
            ContextMember(context_id=ctx.id, user_id=member_user.user_id, role=ContextRole.EDITOR),
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
            admin=mock_admin(),
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
            admin=mock_admin(),
            db=db_session,
        )
        ctx = _pick_context(detail, workspace_admin_no_ctx_member["context_id"])
        assert ctx is not None, "test context must appear in accessible_contexts"
        assert ctx.role == "admin", (
            "workspace admin with no ContextMember row must display 'admin', "
            "not 'owner' (#699 regression pin)"
        )

    @pytest.mark.asyncio
    async def test_workspace_member_no_ctx_member_displays_viewer(
        self,
        db_session: AsyncSession,
        workspace_member_no_ctx_member: dict,
    ) -> None:
        """workspace_member without ContextMember row falls through to the 'viewer' default."""
        detail = await get_user_detail(
            user_id=workspace_member_no_ctx_member["user_id"],
            admin=mock_admin(),
            db=db_session,
        )
        ctx = _pick_context(detail, workspace_member_no_ctx_member["context_id"])
        assert ctx is not None, "test context must appear in accessible_contexts"
        assert ctx.role == "viewer", (
            "workspace member with no ContextMember row must fall through to the "
            "'viewer' default (admin.py:577)"
        )

    @pytest.mark.asyncio
    async def test_workspace_member_with_ctx_member_displays_explicit_role(
        self,
        db_session: AsyncSession,
        workspace_member_with_ctx_member: dict,
    ) -> None:
        detail = await get_user_detail(
            user_id=workspace_member_with_ctx_member["user_id"],
            admin=mock_admin(),
            db=db_session,
        )
        ctx = _pick_context(detail, workspace_member_with_ctx_member["context_id"])
        assert ctx is not None, "test context must appear in accessible_contexts"
        assert ctx.role == "editor", (
            "workspace member with explicit ContextMember(role='editor') must "
            "display the ContextMember role"
        )
