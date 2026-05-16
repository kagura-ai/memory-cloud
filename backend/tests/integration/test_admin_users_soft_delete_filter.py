"""Regression tests for #681 — admin user routes must exclude soft-deleted workspaces.

Same pattern class as #660 / #665 (soft-delete filter omission).

Hits a real Postgres test DB because the existing mock-DB tests in
``tests/api/test_admin_*.py`` cannot detect SQL ``WHERE`` clause omissions.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin import get_user_detail, list_users
from models.auth import User, Workspace, WorkspaceMember


def _mock_admin() -> dict:
    return {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"}


def _new_workspace(*, owner: str, soft_deleted: bool) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"{'deleted' if soft_deleted else 'active'}-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        memory_limit=1000,
        daily_api_limit=500,
        weekly_api_limit=2500,
        deleted_at=(func.now() if soft_deleted else None),
    )


@pytest_asyncio.fixture
async def user_with_mixed_workspaces(db_session: AsyncSession) -> dict:
    """One user owning two ``pro`` workspaces — one active, one soft-deleted."""
    user_id = f"u_{uuid4().hex[:8]}"
    db_session.add(
        User(
            email=f"{user_id}@test.invalid",
            user_id=user_id,
            name="Test User",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    await db_session.flush()

    active = _new_workspace(owner=user_id, soft_deleted=False)
    deleted = _new_workspace(owner=user_id, soft_deleted=True)
    db_session.add_all([active, deleted])
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(workspace_id=active.id, user_id=user_id, role="owner"),
            WorkspaceMember(workspace_id=deleted.id, user_id=user_id, role="owner"),
        ]
    )
    await db_session.commit()

    return {
        "user_id": user_id,
        "active_workspace_id": str(active.id),
        "deleted_workspace_id": str(deleted.id),
    }


# Route-function defaults that mirror the FastAPI Query(...) defaults so we can
# invoke the handlers directly without the dependency-injection machinery.
_LIST_DEFAULTS = {
    "limit": 100,
    "offset": 0,
    "include_workspaces": False,
    "search": None,
    "workspace_id": None,
    "role": None,
    "plan": None,
    "sort": "created_at",
}


class TestListUsersIncludeWorkspaces:
    """``GET /admin/users?include_workspaces=true`` filters soft-deleted (L233-238)."""

    @pytest.mark.asyncio
    async def test_workspaces_array_excludes_soft_deleted(
        self,
        db_session: AsyncSession,
        user_with_mixed_workspaces: dict,
    ) -> None:
        response = await list_users(
            user=_mock_admin(),
            db=db_session,
            **{**_LIST_DEFAULTS, "include_workspaces": True},
        )

        target = next(
            (u for u in response.users if u.id == user_with_mixed_workspaces["user_id"]),
            None,
        )
        assert target is not None, "test user not present in admin listing"

        workspace_ids = {ws["workspace_id"] for ws in target.workspaces}
        assert user_with_mixed_workspaces["active_workspace_id"] in workspace_ids
        assert user_with_mixed_workspaces["deleted_workspace_id"] not in workspace_ids, (
            "soft-deleted workspace must NOT appear in admin user listing (#681)"
        )


class TestGetUserDetail:
    """``GET /admin/users/{user_id}`` ``workspaces`` excludes soft-deleted (L398-404)."""

    @pytest.mark.asyncio
    async def test_workspaces_array_excludes_soft_deleted(
        self,
        db_session: AsyncSession,
        user_with_mixed_workspaces: dict,
    ) -> None:
        detail = await get_user_detail(
            user_id=user_with_mixed_workspaces["user_id"],
            admin=_mock_admin(),
            db=db_session,
        )

        workspace_ids = {ws.workspace_id for ws in detail.workspaces}
        assert user_with_mixed_workspaces["active_workspace_id"] in workspace_ids
        assert user_with_mixed_workspaces["deleted_workspace_id"] not in workspace_ids, (
            "soft-deleted workspace must NOT appear in admin user detail (#681)"
        )
