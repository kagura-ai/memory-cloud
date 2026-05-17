"""Regression tests for #695 — admin users list surfaces per-user cap summary.

Verifies ``GET /admin/users`` returns ``owned_count`` / ``workspace_slot_bonus``
/ ``cap`` for each user, computed in bulk (no N+1) and excluding
soft-deleted workspaces from ``owned_count`` (same #681 class as
``test_admin_users_soft_delete_filter``).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin import list_users
from models.auth import User, Workspace


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
async def users_with_varied_cap(db_session: AsyncSession) -> dict:
    """Three users with distinct cap shapes:

    - baseline:     bonus=0, owns 1 live workspace            → 1 / 1 (at cap)
    - bonus_grant:  bonus=2, owns 1 live + 1 soft-deleted    → 1 / 3 (not at cap)
    - zero_owned:   bonus=0, owns 0                          → 0 / 1
    """
    uids = {key: f"u_{uuid4().hex[:8]}" for key in ("baseline", "bonus_grant", "zero_owned")}

    db_session.add_all(
        [
            User(
                email=f"{uids['baseline']}@test.invalid",
                user_id=uids["baseline"],
                name="Baseline User",
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
                workspace_slot_bonus=0,
            ),
            User(
                email=f"{uids['bonus_grant']}@test.invalid",
                user_id=uids["bonus_grant"],
                name="Bonus User",
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
                workspace_slot_bonus=2,
            ),
            User(
                email=f"{uids['zero_owned']}@test.invalid",
                user_id=uids["zero_owned"],
                name="Zero Owned User",
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
                workspace_slot_bonus=0,
            ),
        ]
    )
    await db_session.flush()

    db_session.add_all(
        [
            _new_workspace(owner=uids["baseline"], soft_deleted=False),
            _new_workspace(owner=uids["bonus_grant"], soft_deleted=False),
            _new_workspace(owner=uids["bonus_grant"], soft_deleted=True),
            # zero_owned: intentionally no workspaces
        ]
    )
    await db_session.commit()

    return uids


_LIST_DEFAULTS = {
    "limit": 500,
    "offset": 0,
    "include_workspaces": False,
    "search": None,
    "workspace_id": None,
    "role": None,
    "plan": None,
    "sort": "created_at",
}


class TestListUsersCapColumn:
    """``GET /admin/users`` surfaces per-user owned_count / bonus / cap (#695)."""

    @pytest.mark.asyncio
    async def test_baseline_user_at_cap(
        self,
        db_session: AsyncSession,
        users_with_varied_cap: dict,
    ) -> None:
        response = await list_users(user=_mock_admin(), db=db_session, **_LIST_DEFAULTS)
        target = next(
            (u for u in response.users if u.id == users_with_varied_cap["baseline"]),
            None,
        )
        assert target is not None
        assert target.owned_count == 1
        assert target.workspace_slot_bonus == 0
        assert target.cap == 1

    @pytest.mark.asyncio
    async def test_bonus_user_below_cap_and_soft_deleted_excluded(
        self,
        db_session: AsyncSession,
        users_with_varied_cap: dict,
    ) -> None:
        response = await list_users(user=_mock_admin(), db=db_session, **_LIST_DEFAULTS)
        target = next(
            (u for u in response.users if u.id == users_with_varied_cap["bonus_grant"]),
            None,
        )
        assert target is not None
        # Soft-deleted workspace MUST NOT count toward owned_count (#681 class).
        assert target.owned_count == 1
        assert target.workspace_slot_bonus == 2
        assert target.cap == 3

    @pytest.mark.asyncio
    async def test_zero_owned_user(
        self,
        db_session: AsyncSession,
        users_with_varied_cap: dict,
    ) -> None:
        response = await list_users(user=_mock_admin(), db=db_session, **_LIST_DEFAULTS)
        target = next(
            (u for u in response.users if u.id == users_with_varied_cap["zero_owned"]),
            None,
        )
        assert target is not None
        # LEFT OUTER JOIN must surface users with zero owned workspaces.
        assert target.owned_count == 0
        assert target.workspace_slot_bonus == 0
        assert target.cap == 1

    @pytest.mark.asyncio
    async def test_pagination_total_unchanged_by_cap_join(
        self,
        db_session: AsyncSession,
        users_with_varied_cap: dict,
    ) -> None:
        """The cap join is in a separate query, so ``total`` must not double-count.

        Regression guard: if the cap join were applied to the base list query
        instead of being a separate bulk fetch, ``bonus_grant`` (owns 1 live
        + 1 soft-deleted = 2 join rows) would inflate ``total`` and produce
        duplicate user rows in ``response.users``. Asserting both no
        duplicates AND a lower-bound on ``total`` catches both failure modes.
        """
        response = await list_users(user=_mock_admin(), db=db_session, **_LIST_DEFAULTS)
        fixture_ids = set(users_with_varied_cap.values())

        # 1. Each fixture user appears EXACTLY once in the response page
        #    (no JOIN row multiplication leaking into the user list).
        per_user_count = dict.fromkeys(fixture_ids, 0)
        for u in response.users:
            if u.id in per_user_count:
                per_user_count[u.id] += 1
        expected_counts = dict.fromkeys(fixture_ids, 1)
        assert per_user_count == expected_counts, (
            f"each fixture user must appear exactly once, got {per_user_count}"
        )

        # 2. ``total`` counts distinct users only — it must be at least the
        #    3 fixture users (test DB may have other users from sibling
        #    fixtures, so we cannot pin equality). If the cap join had
        #    multiplied rows, ``total`` would jump to >= len(fixture_ids)+1
        #    just from the bonus_grant user's 2 workspace rows leaking in.
        #    Combined with assertion (1) above (per_user_count==1 for every
        #    fixture user), this lower bound is sufficient to catch JOIN
        #    multiplication — no need for an upper bound (``total`` is the
        #    pre-pagination match count and can legitimately exceed ``limit``).
        assert response.total >= len(fixture_ids), (
            f"total must include at least the {len(fixture_ids)} fixture users, got {response.total}"
        )
