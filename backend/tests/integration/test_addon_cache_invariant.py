"""Integration tests for admin quota PUT row-based SSoT (#665).

Covers Issue #665 acceptance criteria + locked decisions:

* AC #1 / LD-1 / LD-3: admin grant via ``WorkspaceAddon`` row survives a
  subsequent ``recalculate_workspace_bonuses`` call (the headline bug fix).
* AC #2: admin grant is visible in ``EffectiveQuotaService`` output.
* AC #3 / LD-6: the cross-table invariant
  ``SUM(WorkspaceAddon active * unit_value) == workspace.addon_*_bonus``
  holds after admin-only, Stripe-only, and mixed scenarios — exercised
  via the reusable ``assert_addon_invariant`` helper.
* LD-2: HTTP 400 when admin would silently clamp below the active
  Stripe-purchased floor, and when the admin portion is not divisible
  by the addon's ``unit_value``.
* LD-4 / LD-8: 9-field ``UpdateAddonRequest`` with no-touch semantics on
  the 4 new optional fields; ``AddonValues`` GET response surfaces all 9.
* LD-7: persistent-addon overflow guard (``sleep_contexts`` case) rejects
  reductions that would put current usage above the new effective limit.

Direct-function-call pattern: matches ``test_admin_workspace_slot_bonus``
so the route handler runs against the real Postgres test session — a
MagicMock DB would silently pass on broken WHERE clauses or non-atomic
read-modify-write loops.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_plans import (
    UpdateAddonRequest,
    get_workspace_quotas,
    update_workspace_quotas,
)
from auth.workspace_roles import WorkspaceRole
from models.auth import Context, Workspace, WorkspaceMember
from models.resource import WorkspaceAddon
from services.addon_calculator_service import AddonCalculatorService
from services.effective_quota_service import EffectiveQuotaService
from utils.datetime import utcnow

from ._addon_helpers import assert_addon_invariant
from ._admin_helpers import make_user, make_workspace, mock_admin

# --- fixtures ---------------------------------------------------------------


@pytest_asyncio.fixture
async def pro_workspace(db_session: AsyncSession) -> dict:
    """Bare PRO workspace with an OWNER member, no addon rows."""
    user = make_user(name="Addon Test Owner")
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id, plan_name="pro")
    db_session.add(ws)
    await db_session.flush()

    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=user.user_id, role=WorkspaceRole.OWNER)
    )
    await db_session.commit()

    return {"workspace_id": str(ws.id), "ws_uuid": ws.id, "user_id": user.user_id}


def _zero_legacy_request(**overrides) -> UpdateAddonRequest:
    """Construct an UpdateAddonRequest with the legacy 5 fields all zeroed.

    Avoids the per-test boilerplate of restating all 5 required fields,
    while leaving the new 4 optional fields as ``None`` (no-touch) unless
    a test explicitly sets one. ``overrides`` keyword arguments take
    precedence and may include any of the 9 fields.
    """
    base: dict = {
        "addon_memory_bonus": 0,
        "addon_mcp_quota_bonus": 0,
        "addon_member_bonus": 0,
        "addon_context_bonus": 0,
        "addon_analysis_bonus": 0,
    }
    base.update(overrides)
    return UpdateAddonRequest(**base)


# --- AC #1, LD-1, LD-3 -----------------------------------------------------


class TestAdminGrantSurvivesRecalc:
    """AC #1: admin grant persists across a recalculate_workspace_bonuses call."""

    @pytest.mark.asyncio
    async def test_admin_grant_creates_admin_grant_row(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]

        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_memory_bonus=20000),
            admin_user=mock_admin(),
            db=db_session,
        )

        row_result = await db_session.execute(
            select(WorkspaceAddon).where(
                WorkspaceAddon.workspace_id == ws_uuid,
                WorkspaceAddon.addon_type == "extra_memory",
                WorkspaceAddon.source == "admin_grant",
            )
        )
        admin_row = row_result.scalar_one()
        # 20000 / unit_value 10000 = 2 units
        assert admin_row.quantity == 2
        assert admin_row.source == "admin_grant"
        assert admin_row.active_until is None  # permanent until admin changes
        assert admin_row.created_by == mock_admin()["user_id"]

    @pytest.mark.asyncio
    async def test_admin_grant_survives_independent_recalc(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        """The bug being fixed: a stray recalc must NOT wipe the admin grant."""
        ws_uuid = pro_workspace["ws_uuid"]

        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_memory_bonus=30000),
            admin_user=mock_admin(),
            db=db_session,
        )

        # Simulate an unrelated recalc trigger (e.g. a future Stripe
        # webhook calling recalculate after touching a different addon).
        await AddonCalculatorService(db_session).recalculate_workspace_bonuses(ws_uuid)

        # Re-read the workspace from the DB
        ws_result = await db_session.execute(select(Workspace).where(Workspace.id == ws_uuid))
        workspace = ws_result.scalar_one()
        assert workspace.addon_memory_bonus == 30000  # NOT wiped


# --- AC #2 -----------------------------------------------------------------


class TestEffectiveQuotaReflection:
    """AC #2: admin grant flows through to EffectiveQuotaService output."""

    @pytest.mark.asyncio
    async def test_admin_grant_increases_effective_memory_limit(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]
        before = await EffectiveQuotaService(db_session).get_effective_quotas(ws_uuid)

        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_memory_bonus=50000),
            admin_user=mock_admin(),
            db=db_session,
        )

        after = await EffectiveQuotaService(db_session).get_effective_quotas(ws_uuid)
        assert after["memory_limit"] == before["memory_limit"] + 50000


# --- AC #3, LD-6 ------------------------------------------------------------


class TestAddonInvariant:
    """AC #3: SUM(active WorkspaceAddon rows) == cache column, all addons."""

    @pytest.mark.asyncio
    async def test_invariant_holds_after_admin_only_grant(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]

        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(
                addon_memory_bonus=10000,
                addon_mcp_quota_bonus=5000,
                addon_member_bonus=5,
                addon_context_bonus=10,
                addon_analysis_bonus=2,
                addon_storage_bonus_mb=200,
                addon_sleep_contexts_bonus=3,
            ),
            admin_user=mock_admin(),
            db=db_session,
        )

        await assert_addon_invariant(db_session, ws_uuid)

    @pytest.mark.asyncio
    async def test_invariant_holds_with_mixed_stripe_and_admin(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        """Stripe row + admin row both contribute to the SUM == cache invariant."""
        ws_uuid = pro_workspace["ws_uuid"]

        # Simulate a Stripe purchase (a future webhook would do this).
        db_session.add(
            WorkspaceAddon(
                workspace_id=ws_uuid,
                addon_type="extra_memory",
                source="stripe",
                quantity=3,  # +30000 memory
                purchase_price_cents=10000,
                stripe_product_id="prod_test",
                active_from=utcnow(),
                active_until=None,
                created_by="stripe_webhook",
            )
        )
        await db_session.commit()
        await AddonCalculatorService(db_session).recalculate_workspace_bonuses(ws_uuid)

        # Admin layers an additional grant on top.
        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_memory_bonus=50000),  # 30000 stripe + 20000 admin
            admin_user=mock_admin(),
            db=db_session,
        )

        await assert_addon_invariant(db_session, ws_uuid)

        ws = (
            await db_session.execute(select(Workspace).where(Workspace.id == ws_uuid))
        ).scalar_one()
        assert ws.addon_memory_bonus == 50000


# --- LD-2 ------------------------------------------------------------------


class TestStripeFloorReject:
    """LD-2: admin reductions below the Stripe floor return HTTP 400."""

    @pytest.mark.asyncio
    async def test_reduce_below_stripe_floor_returns_400(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]

        db_session.add(
            WorkspaceAddon(
                workspace_id=ws_uuid,
                addon_type="extra_memory",
                source="stripe",
                quantity=3,  # +30000 memory floor
                purchase_price_cents=10000,
                stripe_product_id="prod_x",
                active_from=utcnow(),
                active_until=None,
                created_by="stripe_webhook",
            )
        )
        await db_session.commit()
        await AddonCalculatorService(db_session).recalculate_workspace_bonuses(ws_uuid)

        with pytest.raises(HTTPException) as exc_info:
            await update_workspace_quotas(
                workspace_id=str(ws_uuid),
                request=_zero_legacy_request(addon_memory_bonus=10000),  # < 30000 floor
                admin_user=mock_admin(),
                db=db_session,
            )

        assert exc_info.value.status_code == 400
        assert "Stripe-purchased floor" in exc_info.value.detail


class TestDivisibilityReject:
    """LD-2 implementation detail: non-multiples of unit_value return HTTP 400."""

    @pytest.mark.asyncio
    async def test_non_multiple_storage_value_returns_400(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]

        # extra_storage unit_value=100MB; 250 is not a multiple
        with pytest.raises(HTTPException) as exc_info:
            await update_workspace_quotas(
                workspace_id=str(ws_uuid),
                request=_zero_legacy_request(addon_storage_bonus_mb=250),
                admin_user=mock_admin(),
                db=db_session,
            )

        assert exc_info.value.status_code == 400
        assert "must be a multiple of" in exc_info.value.detail


# --- LD-7 ------------------------------------------------------------------


class TestPersistentOverflowGuard:
    """LD-7: reducing a persistent addon below current usage returns HTTP 400."""

    @pytest.mark.asyncio
    async def test_reduce_sleep_contexts_below_usage_returns_400(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]
        user_id = pro_workspace["user_id"]

        # Grant 5 extra sleep contexts (effective = pro_base 3 + 5 = 8)
        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_sleep_contexts_bonus=5),
            admin_user=mock_admin(),
            db=db_session,
        )

        # Insert 5 contexts with sleep_mode != 'skip' (direct insert
        # bypasses runtime quota check — we are testing the admin
        # reduce-guard, not the runtime create-guard).
        for _ in range(5):
            db_session.add(
                Context(
                    workspace_id=ws_uuid,
                    name=f"sleep-ctx-{uuid4().hex[:8]}",
                    created_by=user_id,
                    is_private=False,
                    sleep_mode="full",
                )
            )
        await db_session.commit()

        # Attempt to reduce bonus to 0 → effective drops to 3 < 5 usage → 400
        with pytest.raises(HTTPException) as exc_info:
            await update_workspace_quotas(
                workspace_id=str(ws_uuid),
                request=_zero_legacy_request(addon_sleep_contexts_bonus=0),
                admin_user=mock_admin(),
                db=db_session,
            )

        assert exc_info.value.status_code == 400
        assert "sleep_contexts" in exc_info.value.detail


# --- LD-4, LD-8 -------------------------------------------------------------


class TestNoTouchSemantics:
    """LD-4: the 4 new optional fields preserve their value when omitted."""

    @pytest.mark.asyncio
    async def test_omitting_optional_field_does_not_change_it(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]

        # Establish a non-zero baseline on a new (optional) field
        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_storage_bonus_mb=500),
            admin_user=mock_admin(),
            db=db_session,
        )

        ws = (
            await db_session.execute(select(Workspace).where(Workspace.id == ws_uuid))
        ).scalar_one()
        assert ws.addon_storage_bonus_mb == 500

        # Second call: only touch the legacy 5 fields. Storage should NOT change.
        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_memory_bonus=10000),
            admin_user=mock_admin(),
            db=db_session,
        )

        await db_session.refresh(ws)
        assert ws.addon_storage_bonus_mb == 500  # preserved
        assert ws.addon_memory_bonus == 10000  # updated


class TestGetResponseShape:
    """LD-8: GET /admin/plans/workspaces/{id}/quotas exposes all 9 addon fields."""

    @pytest.mark.asyncio
    async def test_get_response_exposes_all_9_addon_fields(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]

        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(
                addon_memory_bonus=10000,
                addon_mcp_quota_bonus=5000,
                addon_member_bonus=5,
                addon_context_bonus=10,
                addon_analysis_bonus=2,
                addon_storage_bonus_mb=300,
                addon_sleep_contexts_bonus=2,
                addon_rest_quota_bonus=1000,
                addon_public_quota_bonus=500,
            ),
            admin_user=mock_admin(),
            db=db_session,
        )

        response = await get_workspace_quotas(
            workspace_id=str(ws_uuid),
            admin_user=mock_admin(),
            db=db_session,
        )

        addon = response.addon
        # All 9 fields surfaced with the values we just set
        assert addon.memory_bonus == 10000
        assert addon.mcp_quota_bonus == 5000
        assert addon.rest_quota_bonus == 1000
        assert addon.public_quota_bonus == 500
        assert addon.member_bonus == 5
        assert addon.context_bonus == 10
        assert addon.analysis_bonus == 2
        assert addon.storage_bonus_mb == 300
        assert addon.sleep_contexts_bonus == 2
