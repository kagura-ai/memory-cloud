"""Shared helpers for admin integration tests.

Used by:
- test_admin_users_soft_delete_filter.py (#681)
- test_admin_users_list_cap_column.py (#695)
- test_admin_workspace_slot_bonus.py (#676)
- test_admin_user_detail_context_role.py (#699)

Each helper returns a fresh, unsaved SQLAlchemy instance with a unique
auto-generated identifier (user_id / workspace UUID / context name) so
fixtures composed from these helpers do not collide across tests.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func

from models.auth import Context, User, Workspace


def mock_admin() -> dict:
    """Mock auth dict matching ``require_admin``'s shape for direct handler invocation."""
    return {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"}


def make_workspace(
    *,
    owner_user_id: str,
    soft_deleted: bool = False,
    plan_name: str = "pro",
) -> Workspace:
    """Build a Workspace with required limits and a pre-assigned UUID."""
    return Workspace(
        id=uuid4(),
        name=f"{'deleted' if soft_deleted else 'active'}-{uuid4().hex[:8]}",
        plan_name=plan_name,
        owner_user_id=owner_user_id,
        memory_limit=1000,
        daily_api_limit=500,
        weekly_api_limit=2500,
        deleted_at=(func.now() if soft_deleted else None),
    )


def make_user(
    *,
    role: str = "user",
    workspace_slot_bonus: int = 0,
    name: str = "Test User",
) -> User:
    """Build a User with a unique user_id and standard oauth provider fields."""
    user_id = f"u_{uuid4().hex[:8]}"
    return User(
        email=f"{user_id}@test.invalid",
        user_id=user_id,
        name=name,
        role=role,
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
        workspace_slot_bonus=workspace_slot_bonus,
    )


def make_context(*, workspace_id, created_by: str, is_private: bool = False) -> Context:
    """Build a non-private Context; the id is server-generated on flush."""
    return Context(
        workspace_id=workspace_id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=created_by,
        is_private=is_private,
    )
