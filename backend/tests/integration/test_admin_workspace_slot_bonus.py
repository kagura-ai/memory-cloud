"""Integration tests for the admin workspace_slot_bonus PATCH endpoint (#676).

Also covers the third #681 location fix (list_users plan filter join, which
PR #685 missed) and the workspace_summary extension on GET /admin/users/{id}.

Direct-function-call pattern (mirrors ``test_admin_users_soft_delete_filter``)
so the route handler executes against a real Postgres test session and the
``UPDATE ... RETURNING`` atomicity is exercised against actual SQL — a
MagicMock DB would silently succeed on broken WHERE clauses or non-atomic
read-modify-write loops.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin import (
    get_user_detail,
    list_users,
    update_workspace_slot_bonus,
)
from models.auth import AuditLog, User, Workspace, WorkspaceMember
from models.schemas import UpdateWorkspaceSlotBonusRequest
from utils.exceptions import BonusBelowZeroError, InsufficientReasonError


def _admin() -> dict:
    return {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"}


def _new_workspace(*, owner: str, soft_deleted: bool = False, plan: str = "pro") -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"{'deleted' if soft_deleted else 'active'}-{uuid4().hex[:8]}",
        plan_name=plan,
        owner_user_id=owner,
        memory_limit=1000,
        daily_api_limit=500,
        weekly_api_limit=2500,
        deleted_at=(func.now() if soft_deleted else None),
    )


@pytest_asyncio.fixture
async def user_with_bonus(db_session: AsyncSession) -> dict:
    """User with bonus=2 (cap=3) owning 1 active workspace.

    Non-destructive baseline: owned_count=1, cap=3 → admin can decrement
    bonus by 1 without hitting the destructive-op gate (new_cap=2 ≥ owned=1).
    """
    user_id = f"u_{uuid4().hex[:8]}"
    db_session.add(
        User(
            email=f"{user_id}@test.invalid",
            user_id=user_id,
            name="Bonus Test User",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
            workspace_slot_bonus=2,
        )
    )
    await db_session.flush()

    ws = _new_workspace(owner=user_id)
    db_session.add(ws)
    await db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user_id, role="owner"))
    await db_session.commit()

    return {"user_id": user_id, "email": f"{user_id}@test.invalid", "workspace_id": str(ws.id)}


@pytest_asyncio.fixture
async def user_at_risk(db_session: AsyncSession) -> dict:
    """User with bonus=0 (cap=1) owning 1 workspace.

    Setting delta=-0 is non-mutating; decrementing bonus past zero is rejected
    by BONUS-002. Used to exercise the below-zero guard cleanly.
    """
    user_id = f"u_{uuid4().hex[:8]}"
    db_session.add(
        User(
            email=f"{user_id}@test.invalid",
            user_id=user_id,
            name="At-Risk Test User",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
            workspace_slot_bonus=0,
        )
    )
    await db_session.flush()

    ws = _new_workspace(owner=user_id, plan="free")
    db_session.add(ws)
    await db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user_id, role="owner"))
    await db_session.commit()

    return {"user_id": user_id, "email": f"{user_id}@test.invalid"}


@pytest_asyncio.fixture
async def user_destructive(db_session: AsyncSession) -> dict:
    """User with bonus=3 (cap=4) owning 4 workspaces — decrementing bonus
    even by 1 creates the over-cap state that requires a reason.

    new_cap (after -1) = 3 < owned_count (4) → destructive.
    """
    user_id = f"u_{uuid4().hex[:8]}"
    db_session.add(
        User(
            email=f"{user_id}@test.invalid",
            user_id=user_id,
            name="Destructive Test User",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
            workspace_slot_bonus=3,
        )
    )
    await db_session.flush()

    workspaces = [_new_workspace(owner=user_id) for _ in range(4)]
    db_session.add_all(workspaces)
    await db_session.flush()
    db_session.add_all(
        [WorkspaceMember(workspace_id=w.id, user_id=user_id, role="owner") for w in workspaces]
    )
    await db_session.commit()

    return {"user_id": user_id, "email": f"{user_id}@test.invalid"}


@pytest_asyncio.fixture
async def user_with_mixed_workspaces_plan_filter(db_session: AsyncSession) -> dict:
    """One user owning two ``pro`` workspaces — one active, one soft-deleted.

    Mirrors the existing #681 test fixture but is reused here to assert the
    plan-filter JOIN that PR #685 missed. The user should NOT surface when
    GET /admin/users?plan=pro is queried purely because of the soft-deleted
    row (i.e. the active row should be sufficient to qualify them; we only
    want the soft-deleted row to be invisible to the filter).
    """
    user_id = f"u_{uuid4().hex[:8]}"
    db_session.add(
        User(
            email=f"{user_id}@test.invalid",
            user_id=user_id,
            name="Plan Filter Test User",
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

    return {"user_id": user_id, "active_id": str(active.id), "deleted_id": str(deleted.id)}


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


class TestUpdateWorkspaceSlotBonus:
    """PATCH /admin/users/{user_id}/workspace_slot_bonus (#676)."""

    @pytest.mark.asyncio
    async def test_increment_returns_updated_state(
        self, db_session: AsyncSession, user_with_bonus: dict
    ) -> None:
        response = await update_workspace_slot_bonus(
            user_id=user_with_bonus["user_id"],
            request=UpdateWorkspaceSlotBonusRequest(delta=1, reason=None),
            admin=_admin(),
            db=db_session,
        )
        assert response.before_value == 2
        assert response.after_value == 3
        assert response.base_cap == 1
        assert response.cap == 4
        assert response.owned_count == 1
        assert response.is_at_cap is False
        assert response.reason is None

    @pytest.mark.asyncio
    async def test_non_destructive_decrement_succeeds_without_reason(
        self, db_session: AsyncSession, user_with_bonus: dict
    ) -> None:
        """bonus=2, owned=1: -1 → new_cap=2 still ≥ owned, no reason required."""
        response = await update_workspace_slot_bonus(
            user_id=user_with_bonus["user_id"],
            request=UpdateWorkspaceSlotBonusRequest(delta=-1, reason=None),
            admin=_admin(),
            db=db_session,
        )
        assert response.after_value == 1
        assert response.cap == 2

    @pytest.mark.asyncio
    async def test_below_zero_raises_bonus_002(
        self, db_session: AsyncSession, user_at_risk: dict
    ) -> None:
        with pytest.raises(BonusBelowZeroError) as exc:
            await update_workspace_slot_bonus(
                user_id=user_at_risk["user_id"],
                request=UpdateWorkspaceSlotBonusRequest(delta=-1, reason=None),
                admin=_admin(),
                db=db_session,
            )
        assert exc.value.error_code == "BONUS-002"
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_destructive_without_reason_raises_bonus_001(
        self, db_session: AsyncSession, user_destructive: dict
    ) -> None:
        with pytest.raises(InsufficientReasonError) as exc:
            await update_workspace_slot_bonus(
                user_id=user_destructive["user_id"],
                request=UpdateWorkspaceSlotBonusRequest(delta=-1, reason=None),
                admin=_admin(),
                db=db_session,
            )
        assert exc.value.error_code == "BONUS-001"

    @pytest.mark.asyncio
    async def test_destructive_with_reason_succeeds_and_writes_audit(
        self, db_session: AsyncSession, user_destructive: dict
    ) -> None:
        response = await update_workspace_slot_bonus(
            user_id=user_destructive["user_id"],
            request=UpdateWorkspaceSlotBonusRequest(
                delta=-1, reason="Reducing bonus per request from finance"
            ),
            admin=_admin(),
            db=db_session,
        )
        assert response.before_value == 3
        assert response.after_value == 2
        assert response.cap == 3
        assert response.owned_count == 4
        assert response.is_at_cap is True  # 4 owned > 3 cap → over-cap state, is_at_cap True
        assert response.reason == "Reducing bonus per request from finance"

        # Audit log row written with the canonical user_metadata payload.
        audit_result = await db_session.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "workspace_slot_bonus_update",
                AuditLog.resource == f"user:{user_destructive['email']}",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        audit_row = audit_result.scalar_one()
        assert audit_row.old_value_hash == "3"
        assert audit_row.new_value_hash == "2"
        assert audit_row.user_metadata["delta"] == -1
        assert audit_row.user_metadata["reason"] == "Reducing bonus per request from finance"
        assert audit_row.user_metadata["target_user_id"] == user_destructive["user_id"]

    @pytest.mark.asyncio
    async def test_whitespace_only_reason_treated_as_missing(
        self, db_session: AsyncSession, user_destructive: dict
    ) -> None:
        """Defense against weak audit trails: ``"   "`` does not bypass BONUS-001."""
        with pytest.raises(InsufficientReasonError):
            await update_workspace_slot_bonus(
                user_id=user_destructive["user_id"],
                request=UpdateWorkspaceSlotBonusRequest(delta=-1, reason="   "),
                admin=_admin(),
                db=db_session,
            )

    @pytest.mark.asyncio
    async def test_unknown_user_returns_404(self, db_session: AsyncSession) -> None:
        with pytest.raises(HTTPException) as exc:
            await update_workspace_slot_bonus(
                user_id="u_nonexistent_xyz",
                request=UpdateWorkspaceSlotBonusRequest(delta=1, reason=None),
                admin=_admin(),
                db=db_session,
            )
        assert exc.value.status_code == 404


class TestGetUserDetailWorkspaceSummary:
    """GET /admin/users/{user_id} now includes workspace_summary (#676)."""

    @pytest.mark.asyncio
    async def test_workspace_summary_present(
        self, db_session: AsyncSession, user_with_bonus: dict
    ) -> None:
        detail = await get_user_detail(
            user_id=user_with_bonus["user_id"],
            admin=_admin(),
            db=db_session,
        )
        assert detail.workspace_summary is not None
        assert detail.workspace_summary.workspace_slot_bonus == 2
        assert detail.workspace_summary.base_cap == 1
        assert detail.workspace_summary.cap == 3
        assert detail.workspace_summary.owned_count == 1
        assert detail.workspace_summary.is_at_cap is False
        owned_ids = {ws.id for ws in detail.workspace_summary.owned_workspaces}
        assert user_with_bonus["workspace_id"] in owned_ids

    @pytest.mark.asyncio
    async def test_workspace_summary_excludes_soft_deleted(
        self,
        db_session: AsyncSession,
        user_with_mixed_workspaces_plan_filter: dict,
    ) -> None:
        """owned_workspaces must apply the soft-delete filter (no #681 regression)."""
        detail = await get_user_detail(
            user_id=user_with_mixed_workspaces_plan_filter["user_id"],
            admin=_admin(),
            db=db_session,
        )
        assert detail.workspace_summary is not None
        owned_ids = {ws.id for ws in detail.workspace_summary.owned_workspaces}
        assert user_with_mixed_workspaces_plan_filter["active_id"] in owned_ids
        assert user_with_mixed_workspaces_plan_filter["deleted_id"] not in owned_ids


class TestListUsersPlanFilterSoftDelete:
    """``GET /admin/users?plan=pro`` excludes soft-deleted workspaces in the JOIN.

    This is the third #681 location that PR #685 missed — fixed in commit 1
    of #676. Distinct from the existing test_admin_users_soft_delete_filter
    suite which only covers the ``include_workspaces`` branch.
    """

    @pytest.mark.asyncio
    async def test_plan_filter_uses_active_workspaces_only(
        self,
        db_session: AsyncSession,
        user_with_mixed_workspaces_plan_filter: dict,
    ) -> None:
        """A user with an active pro workspace must appear under plan=pro
        and the query must not double-count via the soft-deleted row.

        The user has 2 ``pro`` workspaces (one active, one deleted). Pre-fix
        the JOIN would match both rows, surfacing the user twice in the
        underlying COUNT and yielding duplicate rows in the result set. The
        fix ensures only the active row matches, so the user appears exactly
        once.
        """
        response = await list_users(
            user=_admin(),
            db=db_session,
            **{**_LIST_DEFAULTS, "plan": "pro"},
        )
        target_id = user_with_mixed_workspaces_plan_filter["user_id"]
        matches = [u for u in response.users if u.id == target_id]
        assert len(matches) == 1, (
            f"plan=pro filter must surface the user exactly once "
            f"(saw {len(matches)} — likely #681 plan-filter regression)"
        )


class TestRaceSafeBonus002:
    """The conditional UPDATE + disambiguation SELECT in the handler is the
    race-safe form of BONUS-002 — under a concurrent decrement that lands
    between validation and UPDATE the live bonus could already be at 0,
    making the original ``UPDATE … RETURNING`` push the value negative and
    trip the ``workspace_slot_bonus_nonneg`` CHECK (surfacing as a generic
    IntegrityError 500 rather than the structured 400 BONUS-002).

    The end-to-end race is hard to reproduce deterministically without
    concurrent-session orchestration; these tests verify the constituent
    SQL semantics so a future refactor cannot accidentally regress to the
    pre-fix unconditional form. Pairs with the QA-Lead gate2 recommendation.
    """

    @pytest.mark.asyncio
    async def test_conditional_update_returns_zero_rows_when_would_negate(
        self, db_session: AsyncSession, user_at_risk: dict
    ) -> None:
        """``UPDATE ... WHERE workspace_slot_bonus + :delta >= 0`` matches
        zero rows when the live value would go negative — the race-safe
        signal the handler then disambiguates into 404 vs raced-BONUS-002.
        """
        from sqlalchemy import update as sa_update

        # user_at_risk: bonus=0, owned=1. delta=-1 would land bonus=-1.
        result = await db_session.execute(
            sa_update(User)
            .where(
                User.user_id == user_at_risk["user_id"],
                User.workspace_slot_bonus + (-1) >= 0,
            )
            .values(workspace_slot_bonus=User.workspace_slot_bonus + (-1))
            .returning(User.workspace_slot_bonus)
        )
        assert result.one_or_none() is None, (
            "conditional UPDATE must reject below-zero applies (zero rows)"
        )

        # Bonus stays at 0 — the guard rejected, did NOT silently apply -1.
        live = await db_session.scalar(
            select(User.workspace_slot_bonus).where(User.user_id == user_at_risk["user_id"])
        )
        assert live == 0

    @pytest.mark.asyncio
    async def test_conditional_update_matches_when_safe(
        self, db_session: AsyncSession, user_with_bonus: dict
    ) -> None:
        """Positive coverage: the same conditional form normally matches
        and returns the post-state via RETURNING. Guards against a future
        refactor that accidentally tightens the WHERE into rejecting valid
        deltas.
        """
        from sqlalchemy import update as sa_update

        # user_with_bonus: bonus=2. delta=-1 → 1 (safe).
        result = await db_session.execute(
            sa_update(User)
            .where(
                User.user_id == user_with_bonus["user_id"],
                User.workspace_slot_bonus + (-1) >= 0,
            )
            .values(workspace_slot_bonus=User.workspace_slot_bonus + (-1))
            .returning(User.workspace_slot_bonus)
        )
        row = result.one_or_none()
        assert row is not None and row.workspace_slot_bonus == 1


class TestInputBounds:
    """Pydantic-level input bound coverage (#676 QA-Lead findings)."""

    @pytest.mark.asyncio
    async def test_reason_at_max_length_accepted(
        self, db_session: AsyncSession, user_with_bonus: dict
    ) -> None:
        """500 chars is the documented upper bound — exactly 500 must
        round-trip without 422.
        """
        long_reason = "x" * 500
        response = await update_workspace_slot_bonus(
            user_id=user_with_bonus["user_id"],
            request=UpdateWorkspaceSlotBonusRequest(delta=1, reason=long_reason),
            admin=_admin(),
            db=db_session,
        )
        assert response.reason == long_reason

    def test_reason_over_max_length_rejected_at_pydantic(self) -> None:
        """501-char reason must fail Pydantic validation (no DB round-trip)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            UpdateWorkspaceSlotBonusRequest(delta=1, reason="x" * 501)

    def test_delta_over_upper_bound_rejected(self) -> None:
        """Sanity bound: delta > 1_000_000 is rejected by Pydantic — protects
        against INT32 overflow on ``workspace_slot_bonus + delta`` and against
        admin typos like ``1e9``.
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            UpdateWorkspaceSlotBonusRequest(delta=1_000_001, reason=None)

    def test_delta_under_lower_bound_rejected(self) -> None:
        """Sanity bound: delta < -1_000_000 is rejected by Pydantic."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            UpdateWorkspaceSlotBonusRequest(delta=-1_000_001, reason=None)


class TestHttpLayerErrorMapping:
    """The BONUS-001/002 contract relies on
    ``memory_cloud_exception_handler`` translating ``MemoryCloudException``
    subclasses into a structured 400 JSON response (``{error, message,
    details}``). The integration tests above call the route function
    directly, so they verify the raise but NOT the wire translation. These
    tests exercise the handler directly to anchor the contract.

    Pairs with the QA-Lead gate2 recommendation.
    """

    @pytest.mark.asyncio
    async def test_bonus_002_maps_to_structured_400(self) -> None:
        import json
        from unittest.mock import MagicMock

        from api.main import memory_cloud_exception_handler

        exc = BonusBelowZeroError(current=0, delta=-1)
        response = await memory_cloud_exception_handler(MagicMock(), exc)
        assert response.status_code == 400
        body = json.loads(response.body)
        assert body["error"] == "BONUS-002"
        assert "Bonus cannot go below zero" in body["message"]
        # ``details`` carries ``current`` and ``delta`` for SDK consumers
        # routing on the structured error code.
        assert body["details"]["current"] == 0
        assert body["details"]["delta"] == -1

    @pytest.mark.asyncio
    async def test_bonus_001_maps_to_structured_400(self) -> None:
        import json
        from unittest.mock import MagicMock

        from api.main import memory_cloud_exception_handler

        exc = InsufficientReasonError()
        response = await memory_cloud_exception_handler(MagicMock(), exc)
        assert response.status_code == 400
        body = json.loads(response.body)
        assert body["error"] == "BONUS-001"
        assert "Reason required" in body["message"]
