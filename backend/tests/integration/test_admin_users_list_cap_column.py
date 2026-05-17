"""Regression tests for #695 — admin users list surfaces per-user cap summary.

Verifies ``GET /admin/users`` returns ``owned_count`` / ``workspace_slot_bonus``
/ ``cap`` for each user, computed in bulk (no N+1) and excluding
soft-deleted workspaces from ``owned_count`` (same #681 class as
``test_admin_users_soft_delete_filter``).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin import list_users
from models.auth import User

from ._admin_helpers import make_user, make_workspace, mock_admin


@pytest_asyncio.fixture
async def users_with_varied_cap(db_session: AsyncSession) -> dict:
    """Three users with distinct cap shapes:

    - baseline:     bonus=0, owns 1 live workspace            → 1 / 1 (at cap)
    - bonus_grant:  bonus=2, owns 1 live + 1 soft-deleted    → 1 / 3 (not at cap)
    - zero_owned:   bonus=0, owns 0                          → 0 / 1
    """
    baseline = make_user(workspace_slot_bonus=0, name="Baseline User")
    bonus_grant = make_user(workspace_slot_bonus=2, name="Bonus User")
    zero_owned = make_user(workspace_slot_bonus=0, name="Zero Owned User")
    db_session.add_all([baseline, bonus_grant, zero_owned])
    await db_session.flush()

    db_session.add_all(
        [
            make_workspace(owner_user_id=baseline.user_id, soft_deleted=False),
            make_workspace(owner_user_id=bonus_grant.user_id, soft_deleted=False),
            make_workspace(owner_user_id=bonus_grant.user_id, soft_deleted=True),
            # zero_owned: intentionally no workspaces
        ]
    )
    await db_session.commit()

    return {
        "baseline": baseline.user_id,
        "bonus_grant": bonus_grant.user_id,
        "zero_owned": zero_owned.user_id,
    }


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
        response = await list_users(user=mock_admin(), db=db_session, **_LIST_DEFAULTS)
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
        response = await list_users(user=mock_admin(), db=db_session, **_LIST_DEFAULTS)
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
        response = await list_users(user=mock_admin(), db=db_session, **_LIST_DEFAULTS)
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
        response = await list_users(user=mock_admin(), db=db_session, **_LIST_DEFAULTS)
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

        # 2. ``total`` equals the **direct** distinct-user count in the DB.
        #    A bare ``total >= len(fixture_ids)`` lower bound would silently
        #    pass even if JOIN multiplication inflated ``total`` (an
        #    inflated total still satisfies the lower bound). Comparing
        #    against an independent ``SELECT COUNT(*) FROM users`` is the
        #    only check that actually fails when ``list_users``'s count
        #    starts double-counting bonus_grant (1 live + 1 soft-deleted
        #    workspace = 2 join rows if the cap join leaks into the base
        #    query). The direct count uses no joins so it cannot share the
        #    same defect — it is the ground truth.
        direct_count = (await db_session.execute(select(func.count(User.id)))).scalar() or 0
        assert response.total == direct_count, (
            f"response.total ({response.total}) must match the direct distinct user count "
            f"({direct_count}) — a mismatch means the cap join inflated the count via row multiplication"
        )
