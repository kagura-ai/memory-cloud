"""Referral program tests (Issue #1470).

Split in two:

- ``TestEffectiveMemoryLimit`` exercises the entitlement math directly on the
  ORM object (no DB) — that property is the only existing quota path this
  feature changes, so it gets a dedicated regression net.
- The HTTP classes patch the service layer and assert wiring: the deployment
  kill switch, the refusal-to-error-code mapping, and admin-only access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.main import app
from auth.dependencies import require_admin, require_session_auth
from config.plan_tiers import PLAN_TIERS
from config.settings import (
    REFERRAL_TOTAL_PAYOUT_BUDGET_MEMORIES,
    Settings,
    get_settings,
)
from db.base import get_db
from models.auth import Workspace
from services.referral_service import ReferralSummary
from utils.exceptions import (
    ReferralAlreadyRedeemedError,
    ReferralCapReachedError,
    ReferralCodeInvalidError,
    ReferralSelfError,
    ReferralWindowClosedError,
)


def _upper_bound(field_name: str) -> int:
    """Read a ``Field(le=...)`` ceiling off the Settings model.

    Read from the model rather than hardcoded here so that raising a ceiling in
    ``settings.py`` is what trips the bounds tests — a copied number would
    silently agree with any change.
    """
    for constraint in Settings.model_fields[field_name].metadata:
        bound = getattr(constraint, "le", None)
        if bound is not None:
            return int(bound)
    raise AssertionError(f"Settings.{field_name} has no `le=` ceiling — it must be bounded.")


class TestPayoutBudget:
    """The cross-field guard that per-field ``le=`` bounds cannot express (#1470)."""

    def test_payout_budget_matches_the_free_to_basic_gap(self) -> None:
        """Pin the hardcoded budget to the tier values it was derived from.

        ``settings.py`` cannot import ``plan_tiers`` (that module calls
        ``get_settings()`` at import time), so the constant is hardcoded there
        and pinned here instead. If someone retunes the FREE or BASIC memory
        limit, this is what tells them the referral budget moved too.
        """
        assert REFERRAL_TOTAL_PAYOUT_BUDGET_MEMORIES == (
            PLAN_TIERS["basic"].memory_limit - PLAN_TIERS["free"].memory_limit
        )

    def test_per_field_ceilings_alone_would_blow_the_budget(self) -> None:
        """Documents WHY the cross-field validator has to exist.

        If this ever stops being true the validator may look redundant — it is
        not: it is what makes the individually-bounded fields safe *together*.
        """
        product = _upper_bound("referral_referee_reward_memories") * _upper_bound(
            "referral_max_grants_per_referrer"
        )
        assert product > REFERRAL_TOTAL_PAYOUT_BUDGET_MEMORIES

    def test_defaults_are_within_budget(self) -> None:
        settings = get_settings()
        worst = settings.referral_max_grants_per_referrer * max(
            settings.referral_referrer_reward_memories,
            settings.referral_referee_reward_memories,
        )
        assert worst <= REFERRAL_TOTAL_PAYOUT_BUDGET_MEMORIES

    def test_over_budget_config_fails_closed_at_load(self) -> None:
        """An operator cannot dial away the tier ladder one field at a time."""
        with pytest.raises(ValidationError, match="above the"):
            Settings(
                enable_referrals=True,
                referral_max_grants_per_referrer=10,
                referral_referrer_reward_memories=2000,
                referral_referee_reward_memories=2000,
            )

    def test_budget_guard_is_inert_when_referrals_are_disabled(self) -> None:
        """OSS deployments that never enable referrals need not keep these coherent."""
        settings = Settings(
            enable_referrals=False,
            referral_max_grants_per_referrer=10,
            referral_referrer_reward_memories=2000,
            referral_referee_reward_memories=2000,
        )
        assert settings.referral_max_grants_per_referrer == 10

    def test_field_ceilings_still_reject_a_single_absurd_value(self) -> None:
        with pytest.raises(ValidationError):
            Settings(enable_referrals=True, referral_referee_reward_memories=500000)


class TestEffectiveMemoryLimit:
    """``Workspace.effective_memory_limit`` stacks the referral bonus (#1470)."""

    def test_referral_bonus_stacks_on_free(self) -> None:
        ws = Workspace(plan_name="free", addon_memory_bonus=0, referral_memory_bonus=500)
        # FREE base is 1000 -> a felt +50%, which is the whole point of the number.
        assert ws.effective_memory_limit == 1500

    def test_referral_and_addon_bonuses_both_apply(self) -> None:
        ws = Workspace(plan_name="free", addon_memory_bonus=10000, referral_memory_bonus=500)
        assert ws.effective_memory_limit == 11500

    def test_zero_bonus_is_unchanged_from_plan_base(self) -> None:
        ws = Workspace(plan_name="basic", addon_memory_bonus=0, referral_memory_bonus=0)
        assert ws.effective_memory_limit == 10000

    def test_budget_at_the_free_to_basic_gap_does_not_reach_basic(self) -> None:
        """A fully-used chain at the payout budget must stay under BASIC.

        This is the ladder invariant the payout budget exists to hold: the
        referral program must never be a way to get the paid tier for free.
        """
        free_base = Workspace(
            plan_name="free", addon_memory_bonus=0, referral_memory_bonus=0
        ).effective_memory_limit
        basic_base = Workspace(
            plan_name="basic", addon_memory_bonus=0, referral_memory_bonus=0
        ).effective_memory_limit
        assert free_base + REFERRAL_TOTAL_PAYOUT_BUDGET_MEMORIES <= basic_base

    def test_zero_floor_still_applies(self) -> None:
        """A tier that excludes memories cannot be lifted by a referral.

        ``_zero_floor`` is the #569 defense-in-depth guard. The referral bonus is
        summed with the addon bonus *before* that guard precisely so it inherits
        the protection rather than bypassing it.
        """
        ws = Workspace(plan_name="free", addon_memory_bonus=0, referral_memory_bonus=500)
        object.__setattr__(ws, "_plan_tier", MagicMock(memory_limit=0))
        assert ws.effective_memory_limit == 0


def _session_user() -> dict:
    return {"user_id": "user_referee", "email": "referee@test.com", "role": "user"}


def _admin_user() -> dict:
    return {"user_id": "admin_1", "email": "admin@test.com", "role": "admin"}


@pytest.fixture
def client():
    async def mock_session():
        return _session_user()

    async def mock_admin():
        return _admin_user()

    async def mock_db():
        yield MagicMock()

    app.dependency_overrides[require_session_auth] = mock_session
    app.dependency_overrides[require_admin] = mock_admin
    app.dependency_overrides[get_db] = mock_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def referrals_enabled(monkeypatch):
    """Turn the deployment kill switch on for the duration of a test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_referrals", True)
    return settings


def _mock_grant() -> MagicMock:
    return MagicMock(
        id=uuid4(),
        referrer_user_id="user_referrer",
        referrer_workspace_id=uuid4(),
        referred_user_id="user_referee",
        referred_workspace_id=uuid4(),
        referrer_bonus_memories=500,
        referred_bonus_memories=500,
        granted_at=datetime(2026, 8, 1, tzinfo=UTC),
        revoked_at=None,
        revoked_reason=None,
    )


class TestKillSwitch:
    """``enable_referrals`` gates the user-facing surface but not the admin one."""

    def test_summary_404s_when_disabled(self, client, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "enable_referrals", False)
        assert client.get("/api/v1/referrals/me").status_code == 404

    def test_redeem_404s_when_disabled(self, client, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "enable_referrals", False)
        response = client.post("/api/v1/referrals/redeem", json={"code": "abc"})
        assert response.status_code == 404

    def test_admin_ledger_stays_reachable_when_disabled(self, client, monkeypatch) -> None:
        """An operator must be able to unwind grants after hitting the kill switch."""
        monkeypatch.setattr(get_settings(), "enable_referrals", False)
        monkeypatch.setattr(
            "services.referral_service.ReferralService.list_grants",
            AsyncMock(return_value=([], 0)),
        )
        response = client.get("/api/v1/admin/referrals")
        assert response.status_code == 200

    def test_system_info_exposes_the_flag(self, client) -> None:
        features = client.get("/api/v1/system/info").json()["features"]
        assert "referrals" in features

    def test_system_info_does_not_leak_reward_amounts(self, client) -> None:
        """Telling an unauthenticated caller what a fresh account is worth is free
        reconnaissance for a farmer."""
        body = client.get("/api/v1/system/info").text
        assert "reward" not in body.lower()


class TestSummary:
    def test_returns_code_and_remaining_slots(self, client, referrals_enabled, monkeypatch) -> None:
        monkeypatch.setattr(
            "services.referral_service.ReferralService.get_summary",
            AsyncMock(
                return_value=ReferralSummary(
                    code="abc123",
                    max_grants=3,
                    used_grants=1,
                    referee_reward_memories=500,
                    referrer_reward_memories=500,
                    earned_memories=500,
                )
            ),
        )
        body = client.get("/api/v1/referrals/me").json()
        assert body["code"] == "abc123"
        assert body["remaining_grants"] == 2
        assert body["earned_memories"] == 500

    def test_remaining_never_goes_negative(self, client, referrals_enabled, monkeypatch) -> None:
        """The cap can be lowered by config while users sit above it."""
        monkeypatch.setattr(
            "services.referral_service.ReferralService.get_summary",
            AsyncMock(
                return_value=ReferralSummary(
                    code="abc123",
                    max_grants=1,
                    used_grants=4,
                    referee_reward_memories=500,
                    referrer_reward_memories=500,
                    earned_memories=2000,
                )
            ),
        )
        assert client.get("/api/v1/referrals/me").json()["remaining_grants"] == 0


class TestRedeem:
    def test_success_returns_the_grant(self, client, referrals_enabled, monkeypatch) -> None:
        monkeypatch.setattr(
            "services.referral_service.ReferralService.redeem",
            AsyncMock(return_value=_mock_grant()),
        )
        response = client.post("/api/v1/referrals/redeem", json={"code": "abc123"})
        assert response.status_code == 201
        assert response.json()["referred_bonus_memories"] == 500

    @pytest.mark.parametrize(
        ("exc", "expected_code"),
        [
            (ReferralCodeInvalidError(), "REFERRAL-001"),
            (ReferralSelfError(), "REFERRAL-002"),
            (ReferralAlreadyRedeemedError(), "REFERRAL-003"),
            (ReferralWindowClosedError(window_hours=72), "REFERRAL-004"),
            (ReferralCapReachedError(), "REFERRAL-005"),
        ],
    )
    def test_refusals_map_to_400_with_routable_codes(
        self, client, referrals_enabled, monkeypatch, exc, expected_code
    ) -> None:
        monkeypatch.setattr(
            "services.referral_service.ReferralService.redeem",
            AsyncMock(side_effect=exc),
        )
        response = client.post("/api/v1/referrals/redeem", json={"code": "abc123"})
        assert response.status_code == 400
        assert expected_code in response.text

    def test_invalid_code_and_cap_reached_are_indistinguishable_to_the_caller(
        self,
    ) -> None:
        """Probing a code must not reveal whether it exists or who owns it."""
        assert ReferralCodeInvalidError().message == ReferralCapReachedError().message

    def test_empty_code_is_rejected_before_the_service(self, client, referrals_enabled) -> None:
        assert client.post("/api/v1/referrals/redeem", json={"code": ""}).status_code == 422


class TestAdminRevoke:
    def test_revoke_requires_a_reason(self, client) -> None:
        response = client.post(f"/api/v1/admin/referrals/{uuid4()}/revoke", json={"reason": ""})
        assert response.status_code == 422

    def test_revoke_404s_for_unknown_grant(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            "services.referral_service.ReferralService.revoke",
            AsyncMock(return_value=None),
        )
        response = client.post(
            f"/api/v1/admin/referrals/{uuid4()}/revoke", json={"reason": "abuse"}
        )
        assert response.status_code == 404

    def test_non_admin_is_rejected(self) -> None:
        """The signup-gate router never got this test; this one does.

        No ``with`` block: entering the TestClient context runs the app lifespan,
        which needs a live Redis. These routes are reachable without it.
        """

        async def mock_db():
            yield MagicMock()

        app.dependency_overrides[get_db] = mock_db
        try:
            unauth = TestClient(app, raise_server_exceptions=False)
            assert unauth.get("/api/v1/admin/referrals").status_code in (401, 403)
            assert unauth.post(
                f"/api/v1/admin/referrals/{uuid4()}/revoke", json={"reason": "x"}
            ).status_code in (401, 403)
        finally:
            app.dependency_overrides.clear()
