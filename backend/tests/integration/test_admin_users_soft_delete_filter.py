"""Regression tests for #681 — admin user routes must exclude soft-deleted workspaces.

Same pattern class as #660 / #665 (soft-delete filter omission).

Hits a real Postgres test DB because the existing mock-DB tests in
``tests/api/test_admin_*.py`` cannot detect SQL ``WHERE`` clause omissions.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin import get_user_detail, list_users
from models.auth import WorkspaceMember

from ._admin_helpers import make_user, make_workspace, mock_admin


@pytest_asyncio.fixture
async def user_with_mixed_workspaces(db_session: AsyncSession) -> dict:
    """One user owning two ``pro`` workspaces — one active, one soft-deleted."""
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    active = make_workspace(owner_user_id=user.user_id, soft_deleted=False)
    deleted = make_workspace(owner_user_id=user.user_id, soft_deleted=True)
    db_session.add_all([active, deleted])
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(workspace_id=active.id, user_id=user.user_id, role="owner"),
            WorkspaceMember(workspace_id=deleted.id, user_id=user.user_id, role="owner"),
        ]
    )
    await db_session.commit()

    return {
        "user_id": user.user_id,
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
            user=mock_admin(),
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
            admin=mock_admin(),
            db=db_session,
        )

        workspace_ids = {ws.workspace_id for ws in detail.workspaces}
        assert user_with_mixed_workspaces["active_workspace_id"] in workspace_ids
        assert user_with_mixed_workspaces["deleted_workspace_id"] not in workspace_ids, (
            "soft-deleted workspace must NOT appear in admin user detail (#681)"
        )
