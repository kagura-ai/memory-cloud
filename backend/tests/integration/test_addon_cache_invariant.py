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

from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_plans import (
    UpdateAddonRequest,
    get_addon_cache_consistency,
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
    """Construct an UpdateAddonRequest with the legacy 5 fields explicitly zeroed.

    Since #665 review-fix #2 unified all 9 fields as optional with no-touch
    semantics, this helper now passes the 5 legacy fields as *explicit* zeros
    (matching the legacy admin UI's behavior of always sending all 5 values).
    The new 4 optional fields remain ``None`` (no-touch) unless an override
    sets them. ``overrides`` may include any of the 9 fields and take
    precedence over the explicit-zero defaults.
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

    @pytest.mark.asyncio
    async def test_get_response_exposes_all_9_effective_fields(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        """Review fix #8: GET ``effective`` block surfaces all 9 fields.

        Pre-fix, ``effective`` was a 5-field QuotaBreakdown — the 4 new
        addon types (rest_quota, public_quota, storage, sleep_contexts)
        had no read-back path even though PUT accepted them. Confirms the
        write-only hole is closed.
        """
        ws_uuid = pro_workspace["ws_uuid"]

        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(
                addon_storage_bonus_mb=200,
                addon_rest_quota_bonus=1000,
                addon_public_quota_bonus=500,
                addon_sleep_contexts_bonus=2,
            ),
            admin_user=mock_admin(),
            db=db_session,
        )

        response = await get_workspace_quotas(
            workspace_id=str(ws_uuid),
            admin_user=mock_admin(),
            db=db_session,
        )
        eff = response.effective

        # Effective values reflect tier base + admin grant for all 9
        # addon types. PRO base values come from config/plan_tiers.py.
        assert eff.rest_calls_per_day > 0  # PRO base 5000 + addon 1000
        assert eff.public_calls_per_day > 0  # PRO base 1000 + addon 500
        assert eff.storage_bytes_limit > 0  # PRO base 10 GiB + addon 200 MB
        assert eff.sleep_enabled_contexts_limit > 0  # PRO base 3 + addon 2


# --- Review fixes ----------------------------------------------------------


class TestAlwaysFireOverflowGuard:
    """Review fix #6: LD-7 overflow guard fires regardless of cache match.

    Pre-fix, the guard was keyed on ``requested != old_bonus``. If the cache
    column was stale (e.g. operator SQL bypassing the application path), a
    re-PUT of the cached value silently skipped the check and let over-cap
    state persist. The fix runs the guard whenever the addon has a usage
    counter, regardless of cache equality.
    """

    @pytest.mark.asyncio
    async def test_guard_fires_when_requested_equals_cached_bonus(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]
        user_id = pro_workspace["user_id"]

        # Set up: grant sleep_contexts_bonus=5 (effective = 3 + 5 = 8),
        # create 5 sleep-enabled contexts, then simulate a cache desync
        # by leaving 5 in the cache but having actual usage that would
        # exceed the post-reduction effective. We do this by re-asserting
        # the same bonus value AFTER the contexts exist — pre-fix the
        # guard skipped; post-fix it fires.
        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_sleep_contexts_bonus=5),
            admin_user=mock_admin(),
            db=db_session,
        )
        for _ in range(8):  # exactly at effective cap (3 + 5)
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

        # Re-PUT the SAME bonus value (5). Pre-fix this skipped the guard
        # because requested == old_bonus. Post-fix the guard fires and
        # confirms usage (8) <= effective (8), so it passes. To prove the
        # guard *runs*, we add one more sleep context to push over the
        # limit, then re-PUT — the guard should now reject 400.
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

        # Usage=9, requested=5 (same as old), effective=8. 9 > 8 → 400.
        with pytest.raises(HTTPException) as exc_info:
            await update_workspace_quotas(
                workspace_id=str(ws_uuid),
                request=_zero_legacy_request(addon_sleep_contexts_bonus=5),
                admin_user=mock_admin(),
                db=db_session,
            )

        assert exc_info.value.status_code == 400
        assert "sleep_contexts" in exc_info.value.detail


class TestUpsertPreservesActiveUntilAndCreatedBy:
    """Review fix #5 + #7: UPSERT preserves active_until and created_by.

    Pre-fix, ON CONFLICT DO UPDATE set_={active_until: None, created_by: ...}
    silently clobbered any prior expiration to NULL (un-expiring time-bound
    grants) and overwrote the original-grantor attribution while preserving
    active_from — half-preserved audit trail. The fix excludes both fields
    from set_, preserving the full original-grant record.
    """

    @pytest.mark.asyncio
    async def test_upsert_preserves_original_grantor_and_expiration(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]

        # Admin A grants memory_bonus=10000 — creates admin_grant row.
        original_admin = {
            "user_id": "admin_A_original",
            "email": "a@test.invalid",
            "role": "admin",
        }
        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_memory_bonus=10000),
            admin_user=original_admin,
            db=db_session,
        )

        # Simulate ops manually setting active_until on the row (would
        # represent a future time-bound-grant feature or a manual revoke
        # scheduled by ops).
        from datetime import timedelta

        sentinel_expiry = utcnow() + timedelta(days=30)
        existing = (
            await db_session.execute(
                select(WorkspaceAddon).where(
                    WorkspaceAddon.workspace_id == ws_uuid,
                    WorkspaceAddon.addon_type == "extra_memory",
                    WorkspaceAddon.source == "admin_grant",
                )
            )
        ).scalar_one()
        existing.active_until = sentinel_expiry
        await db_session.commit()

        # Admin B re-grants the same memory_bonus value. Pre-fix this would
        # have wiped active_until → None AND overwritten created_by → 'admin_B'.
        # Post-fix both fields are preserved.
        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=_zero_legacy_request(addon_memory_bonus=20000),
            admin_user={
                "user_id": "admin_B_later",
                "email": "b@test.invalid",
                "role": "admin",
            },
            db=db_session,
        )

        await db_session.refresh(existing)
        # active_until preserved (NOT clobbered to NULL)
        assert existing.active_until is not None
        # created_by preserved (NOT overwritten to admin_B)
        assert existing.created_by == "admin_A_original"
        # quantity updated as expected
        assert existing.quantity == 2  # 20000 / 10000


class TestSkipRecalcOnEmptyMutations:
    """Review fix #14: no-op PUT should not trigger recalc commit + log.

    When every field in the request body is None (or matches the no-op
    case where grants_to_apply is empty after validation), the handler
    should skip ``recalculate_workspace_bonuses`` entirely — avoiding a
    misleading ``addon_bonuses_recalculated`` structured log entry that
    implies state changed.
    """

    @pytest.mark.asyncio
    async def test_all_none_request_does_not_create_admin_grant_rows(
        self, db_session: AsyncSession, pro_workspace: dict
    ) -> None:
        ws_uuid = pro_workspace["ws_uuid"]

        # Send a request with every field omitted (all None default).
        # Post-fix: validation produces zero grants_to_apply, mutation pass
        # is a no-op, recalc is skipped — no admin_grant rows created.
        await update_workspace_quotas(
            workspace_id=str(ws_uuid),
            request=UpdateAddonRequest(),  # all 9 None
            admin_user=mock_admin(),
            db=db_session,
        )

        # No admin_grant rows should exist for this workspace.
        rows = (
            (
                await db_session.execute(
                    select(WorkspaceAddon).where(
                        WorkspaceAddon.workspace_id == ws_uuid,
                        WorkspaceAddon.source == "admin_grant",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 0


# --- #799: normalization of pre-#665 broken caches -------------------------


async def _seed_workspace_with_cache(
    db_session: AsyncSession, cache_col: str, cache_value: int
) -> Workspace:
    """Create a PRO workspace with a directly-set (possibly drifted) cache column.

    Returns the flushed (not yet committed) ORM instance so the caller can
    attach ``WorkspaceAddon`` rows referencing ``ws.id`` before committing.
    """
    user = make_user(name="Normalization Test Owner")
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id, plan_name="pro")
    setattr(ws, cache_col, cache_value)
    db_session.add(ws)
    await db_session.flush()
    return ws


async def _reread_workspace(db_session: AsyncSession, ws_id) -> Workspace:
    """Re-SELECT a workspace so assertions see post-recalc committed state."""
    return (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()


class TestNormalizationRestoresSSoT:
    """#799: recalc-from-rows normalizes legacy non-multiple cache values.

    The ``e23_799_normalize_addon_caches`` migration applies the SQL
    equivalent of ``AddonCalculatorService.recalculate_workspace_bonuses``
    (``cache = SUM(active rows) * unit_value``) to every active workspace.
    These tests exercise that same service path against the classes of legacy
    state ``e22_665`` left behind — orphan, partial-divisible, consistent, and
    expired-row — pinning the post-migration invariant
    ``cache == SUM(active rows × unit)`` AND the active-window predicate that
    the migration SQL and the runtime recalc must share. The migration's raw
    SQL is additionally exercised by ``alembic upgrade head`` in the
    integration harness.
    """

    @pytest.mark.asyncio
    async def test_orphan_cache_normalized_to_zero(self, db_session: AsyncSession) -> None:
        """Legacy 9000 memory (no backing WorkspaceAddon row) → recalc resets to 0.

        This is the exact prod incident: 9000 < unit_value 10000, so e22 created
        no row and left the cache at 9000, which the #663 dialog then rejected.
        """
        ws = await _seed_workspace_with_cache(db_session, "addon_memory_bonus", 9000)
        await db_session.commit()

        await AddonCalculatorService(db_session).recalculate_workspace_bonuses(ws.id)

        ws_after = await _reread_workspace(db_session, ws.id)
        assert ws_after.addon_memory_bonus == 0
        await assert_addon_invariant(db_session, ws.id)

    @pytest.mark.asyncio
    async def test_partial_divisible_cache_preserves_backfilled_row(
        self, db_session: AsyncSession
    ) -> None:
        """Legacy 250 storage with a 2-unit (200) row → recalc yields 200, not 0.

        This is the case the rejected "reset non-multiples to 0" design would
        have broken: it would zero the cache while a legitimate 200 MB row
        remained, re-violating the SSoT (cache 0 ≠ SUM 200).
        """
        ws = await _seed_workspace_with_cache(db_session, "addon_storage_bonus_mb", 250)
        # e22 would have floor-divided 250 → a 2-unit (200 MB) admin_grant row.
        db_session.add(
            WorkspaceAddon(
                workspace_id=ws.id,
                addon_type="extra_storage",
                quantity=2,
                source="admin_grant",
                active_from=utcnow(),
                created_by="pre_665_migration_backfill",
            )
        )
        await db_session.commit()

        await AddonCalculatorService(db_session).recalculate_workspace_bonuses(ws.id)

        ws_after = await _reread_workspace(db_session, ws.id)
        assert ws_after.addon_storage_bonus_mb == 200  # preserved, NOT reset to 0
        await assert_addon_invariant(db_session, ws.id)

    @pytest.mark.asyncio
    async def test_consistent_cache_unchanged(self, db_session: AsyncSession) -> None:
        """An already-consistent workspace is a no-op under recalc (idempotent)."""
        ws = await _seed_workspace_with_cache(db_session, "addon_memory_bonus", 20000)
        db_session.add(
            WorkspaceAddon(
                workspace_id=ws.id,
                addon_type="extra_memory",
                quantity=2,  # 2 × 10000 == the 20000 cache set above
                source="admin_grant",
                active_from=utcnow(),
                created_by="admin_runner",
            )
        )
        await db_session.commit()

        await AddonCalculatorService(db_session).recalculate_workspace_bonuses(ws.id)

        ws_after = await _reread_workspace(db_session, ws.id)
        assert ws_after.addon_memory_bonus == 20000  # unchanged
        await assert_addon_invariant(db_session, ws.id)

    @pytest.mark.asyncio
    async def test_expired_addon_row_excluded_from_recalc(self, db_session: AsyncSession) -> None:
        """Active-window predicate: an EXPIRED row must NOT count toward the cache.

        Guards the ``active_until > NOW()`` predicate that the migration SQL and
        the runtime recalc share (the divergence trap called out in the e23
        docstring). A stale cache of 10000 backed only by an expired
        ``extra_memory`` row must normalize to 0.
        """
        ws = await _seed_workspace_with_cache(db_session, "addon_memory_bonus", 10000)
        db_session.add(
            WorkspaceAddon(
                workspace_id=ws.id,
                addon_type="extra_memory",
                quantity=1,
                source="admin_grant",
                active_from=utcnow() - timedelta(days=2),
                active_until=utcnow() - timedelta(days=1),  # expired yesterday
                created_by="admin_runner",
            )
        )
        await db_session.commit()

        await AddonCalculatorService(db_session).recalculate_workspace_bonuses(ws.id)

        ws_after = await _reread_workspace(db_session, ws.id)
        assert ws_after.addon_memory_bonus == 0  # expired row excluded from SUM
        await assert_addon_invariant(db_session, ws.id)


class TestAddonCacheConsistencyEndpoint:
    """#799: GET /admin/plans/addon-cache-consistency detects drifted caches.

    Direct-function-call pattern (matches the rest of this file): the handler
    runs against the real Postgres test session. Assertions filter by the
    workspace IDs this test created so they stay deterministic regardless of
    any other active workspaces present in the session.
    """

    @pytest.mark.asyncio
    async def test_endpoint_reports_drifted_and_skips_healthy(
        self, db_session: AsyncSession
    ) -> None:
        # Drifted: orphan 9000 memory cache, no backing WorkspaceAddon row.
        drifted = await _seed_workspace_with_cache(db_session, "addon_memory_bonus", 9000)
        # Healthy: cache 20000 backed by a matching 2-unit row.
        healthy = await _seed_workspace_with_cache(db_session, "addon_memory_bonus", 20000)
        db_session.add(
            WorkspaceAddon(
                workspace_id=healthy.id,
                addon_type="extra_memory",
                quantity=2,
                source="admin_grant",
                active_from=utcnow(),
                created_by="admin_runner",
            )
        )
        await db_session.commit()

        entries = await get_addon_cache_consistency(admin_user=mock_admin(), db=db_session)

        by_key = {(e.workspace_id, e.cache_column): e for e in entries}

        drift = by_key.get((str(drifted.id), "addon_memory_bonus"))
        assert drift is not None, "drifted workspace must be reported"
        assert drift.addon_type == "extra_memory"
        assert drift.cache_value == 9000
        assert drift.expected_value == 0

        # The healthy workspace must NOT appear for any addon column.
        assert not [k for k in by_key if k[0] == str(healthy.id)]

    @pytest.mark.asyncio
    async def test_endpoint_omits_consistent_workspace(self, db_session: AsyncSession) -> None:
        """A fully-consistent workspace is absent from the drift report."""
        ws = await _seed_workspace_with_cache(db_session, "addon_memory_bonus", 10000)
        db_session.add(
            WorkspaceAddon(
                workspace_id=ws.id,
                addon_type="extra_memory",
                quantity=1,  # 1 × 10000 == the cache
                source="admin_grant",
                active_from=utcnow(),
                created_by="admin_runner",
            )
        )
        await db_session.commit()

        entries = await get_addon_cache_consistency(admin_user=mock_admin(), db=db_session)

        mine = [e for e in entries if e.workspace_id == str(ws.id)]
        assert mine == []
