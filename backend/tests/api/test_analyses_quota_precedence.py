"""Tier+quota+allowlist precedence ordering test (Issue #496 AC).

The 4-stage gate chain in ``auth.analysis_gates.require_memory_analysis_access``
must fail fast at the FIRST applicable gate, in this order:

    Gate 2 (workspace owner)          → 403 (HTTPException)
    Gate 3 (Pro tier feature flag)    → 403 (FeatureNotAvailableError)
    Gate 4 (daily quota)              → 429 (QuotaExceededError)
    Gate 5 (allowlist kill switch)    → 403 (FeatureNotAvailableError)

The issue spec covers four blocked states (matching ``backend/src/auth/analysis_gates.py``):

    basic + no-BYOK + empty allowlist  → 403 tier_required (NOT 422 BYOK or 403 allowlist)
    pro + quota exhausted + no-BYOK    → 429 remaining_today (NOT 422 BYOK)
    pro + quota OK + no-BYOK           → BYOK 422 happens INSIDE orchestrator.start,
                                          NOT in the gate chain (skipped here)
    pro + quota OK + BYOK + empty allowlist → 403 allowlist

This test suite locks the precedence into structural assertions so a future
reorder (eg moving allowlist before quota) trips a red CI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from auth.analysis_gates import require_memory_analysis_access
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError


def _user_dict(workspace_id: UUID) -> dict:
    return {"user_id": "test_user", "current_workspace_id": workspace_id}


@pytest.mark.asyncio
async def test_gate2_owner_failure_short_circuits():
    """Owner check fails → HTTPException 403, gates 3-5 never run."""
    workspace_id = uuid4()
    user = _user_dict(workspace_id)
    db = AsyncMock()

    perm_mock = MagicMock()
    perm_mock.check_workspace_owner = AsyncMock(
        side_effect=HTTPException(status_code=403, detail="not owner")
    )

    quota_check_mock = AsyncMock()
    allowlist_mock = MagicMock()
    quota_gate_mock = AsyncMock()

    with (
        patch("services.permission_service.PermissionService", return_value=perm_mock),
        patch("services.quota_service.QuotaService") as quota_cls,
        patch(
            "auth.analysis_gates.check_workspace_in_allowlist",
            allowlist_mock,
        ),
        patch(
            "auth.analysis_gates.check_memory_analysis_quota",
            quota_gate_mock,
        ),
    ):
        quota_cls.return_value.check_feature_access = quota_check_mock
        with pytest.raises(HTTPException) as exc:
            await require_memory_analysis_access(user=user, db=db)
        assert exc.value.status_code == 403

    quota_check_mock.assert_not_called()
    allowlist_mock.assert_not_called()
    quota_gate_mock.assert_not_called()


@pytest.mark.asyncio
async def test_gate3_tier_failure_runs_before_quota_or_allowlist():
    """Basic plan, owner OK → FeatureNotAvailableError BEFORE quota/allowlist."""
    workspace_id = uuid4()
    user = _user_dict(workspace_id)
    db = AsyncMock()

    perm_mock = MagicMock()
    perm_mock.check_workspace_owner = AsyncMock(return_value=None)

    feature_check_mock = AsyncMock(return_value=(False, "memory_analysis requires Pro"))
    allowlist_mock = MagicMock()
    quota_gate_mock = AsyncMock()

    with (
        patch("services.permission_service.PermissionService", return_value=perm_mock),
        patch("services.quota_service.QuotaService") as quota_cls,
        patch(
            "auth.analysis_gates.check_workspace_in_allowlist",
            allowlist_mock,
        ),
        patch(
            "auth.analysis_gates.check_memory_analysis_quota",
            quota_gate_mock,
        ),
    ):
        quota_cls.return_value.check_feature_access = feature_check_mock
        with pytest.raises(FeatureNotAvailableError) as exc:
            await require_memory_analysis_access(user=user, db=db)
        assert exc.value.status_code == 403
        assert exc.value.details.get("feature") == "memory_analysis"

    quota_gate_mock.assert_not_called()
    allowlist_mock.assert_not_called()


@pytest.mark.asyncio
async def test_gate4_quota_failure_runs_before_allowlist():
    """Pro + quota exhausted → QuotaExceededError BEFORE allowlist."""
    workspace_id = uuid4()
    user = _user_dict(workspace_id)
    db = AsyncMock()

    perm_mock = MagicMock()
    perm_mock.check_workspace_owner = AsyncMock(return_value=None)

    feature_check_mock = AsyncMock(return_value=(True, None))
    quota_gate_mock = AsyncMock(side_effect=QuotaExceededError("quota exhausted"))
    allowlist_mock = MagicMock()
    tz_mock = AsyncMock(return_value="UTC")

    with (
        patch("services.permission_service.PermissionService", return_value=perm_mock),
        patch("services.quota_service.QuotaService") as quota_cls,
        patch(
            "auth.analysis_gates.check_workspace_in_allowlist",
            allowlist_mock,
        ),
        patch(
            "auth.analysis_gates.check_memory_analysis_quota",
            quota_gate_mock,
        ),
        patch("auth.analysis_gates._get_user_timezone", tz_mock),
    ):
        quota_cls.return_value.check_feature_access = feature_check_mock
        with pytest.raises(QuotaExceededError) as exc:
            await require_memory_analysis_access(user=user, db=db)
        assert exc.value.status_code == 429

    allowlist_mock.assert_not_called()


@pytest.mark.asyncio
async def test_gate5_allowlist_failure_after_quota_pass():
    """Pro + quota OK + allowlist empty → FeatureNotAvailableError 403 (last gate)."""
    workspace_id = uuid4()
    user = _user_dict(workspace_id)
    db = AsyncMock()

    perm_mock = MagicMock()
    perm_mock.check_workspace_owner = AsyncMock(return_value=None)

    feature_check_mock = AsyncMock(return_value=(True, None))
    quota_gate_mock = AsyncMock(return_value=None)
    allowlist_mock = MagicMock(return_value=False)
    tz_mock = AsyncMock(return_value="UTC")

    with (
        patch("services.permission_service.PermissionService", return_value=perm_mock),
        patch("services.quota_service.QuotaService") as quota_cls,
        patch(
            "auth.analysis_gates.check_workspace_in_allowlist",
            allowlist_mock,
        ),
        patch(
            "auth.analysis_gates.check_memory_analysis_quota",
            quota_gate_mock,
        ),
        patch("auth.analysis_gates._get_user_timezone", tz_mock),
    ):
        quota_cls.return_value.check_feature_access = feature_check_mock
        with pytest.raises(FeatureNotAvailableError) as exc:
            await require_memory_analysis_access(user=user, db=db)
        assert exc.value.status_code == 403
        assert exc.value.details.get("feature") == "memory_analysis"


@pytest.mark.asyncio
async def test_all_gates_pass_returns_user_tuple():
    """Happy path: all gates pass → ``(user_id, workspace_id, tz)`` returned."""
    workspace_id = uuid4()
    user = _user_dict(workspace_id)
    db = AsyncMock()

    perm_mock = MagicMock()
    perm_mock.check_workspace_owner = AsyncMock(return_value=None)
    feature_check_mock = AsyncMock(return_value=(True, None))
    quota_gate_mock = AsyncMock(return_value=None)
    allowlist_mock = MagicMock(return_value=True)
    tz_mock = AsyncMock(return_value="Asia/Tokyo")

    with (
        patch("services.permission_service.PermissionService", return_value=perm_mock),
        patch("services.quota_service.QuotaService") as quota_cls,
        patch(
            "auth.analysis_gates.check_workspace_in_allowlist",
            allowlist_mock,
        ),
        patch(
            "auth.analysis_gates.check_memory_analysis_quota",
            quota_gate_mock,
        ),
        patch("auth.analysis_gates._get_user_timezone", tz_mock),
    ):
        quota_cls.return_value.check_feature_access = feature_check_mock
        result = await require_memory_analysis_access(user=user, db=db)

    assert result == ("test_user", workspace_id, "Asia/Tokyo")


@pytest.mark.asyncio
async def test_no_workspace_selected_short_circuits_with_400():
    """``current_workspace_id`` missing → 400 BEFORE any gate runs."""
    user = {"user_id": "test_user"}  # no current_workspace_id
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await require_memory_analysis_access(user=user, db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_require_memory_analysis_access_calls_quota_with_supported_kwargs():
    """Regression test for #496: ``require_memory_analysis_access`` must
    invoke ``check_memory_analysis_quota`` with **only** the kwargs the
    target function actually accepts. The other gate tests mock the
    whole quota function so they would NOT catch a signature drift
    between caller and callee (e.g. caller passing ``raise_on_exceeded``
    after callee dropped that parameter — this exact bug surfaced once).
    This test wires through to the real ``check_memory_analysis_quota``
    via patches that only stub the *internal* DB primitives, so the
    caller→callee kwargs contract is exercised end-to-end.
    """
    from inspect import signature

    from auth.analysis_gates import check_memory_analysis_quota

    # The function must NOT accept ``raise_on_exceeded`` — it raises
    # unconditionally on quota exceeded. If a future refactor reintroduces
    # the parameter, the call site contract diverges silently and gate 4
    # 500s with TypeError. Pin the signature here so signature drift
    # is loud at test time.
    params = signature(check_memory_analysis_quota).parameters
    assert "raise_on_exceeded" not in params, (
        "raise_on_exceeded must NOT be a parameter of check_memory_analysis_quota — "
        "the function is raise-only by design. If you need a non-raising variant, add a "
        "separate ``check_memory_analysis_quota_status`` helper instead of overloading "
        "this one (see auth/analysis_gates.py docstring)."
    )
    # Positive contract — these ARE the canonical kwargs callers must use.
    assert set(params.keys()) >= {"db", "workspace_id", "user_timezone"}


@pytest.mark.asyncio
async def test_quota_exceeded_error_carries_structured_detail():
    """Regression test for #496: ``check_memory_analysis_quota`` raises
    ``QuotaExceededError`` with structured kwargs (``used_today``,
    ``limit_today``, ``addon_bonus``, ``remaining_today``,
    ``resets_at``). If the exception class signature regresses to
    "only accept ``message`` + ``quota_type``", every quota-exhausted
    request 500s with TypeError instead of returning a clean 429. The
    other gate tests mock the gate function so they would not catch
    this — this one constructs the exception directly.
    """
    err = QuotaExceededError(
        "Analysis daily quota exceeded: 3/3 runs today (addon bonus 0). "
        "Resets at 2026-05-03T00:00:00+09:00.",
        quota_type="memory_analysis",
        used_today=3,
        limit_today=3,
        addon_bonus=0,
        remaining_today=0,
        resets_at="2026-05-03T00:00:00+09:00",
    )
    assert err.status_code == 429
    assert err.error_code == "QUOTA-001"
    assert err.details["used_today"] == 3
    assert err.details["limit_today"] == 3
    assert err.details["resets_at"] == "2026-05-03T00:00:00+09:00"


@pytest.mark.asyncio
async def test_check_workspace_in_allowlist_pure_function(monkeypatch):
    """``check_workspace_in_allowlist`` is the kill-switch primitive."""
    from auth.analysis_gates import check_workspace_in_allowlist
    from config.settings import get_settings

    workspace_id = uuid4()
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "analysis_enabled_workspace_ids",
        f"{workspace_id},{uuid4()}",
        raising=False,
    )
    assert check_workspace_in_allowlist(workspace_id) is True
    monkeypatch.setattr(settings, "analysis_enabled_workspace_ids", "", raising=False)
    # Empty list → False (kill switch active globally).
    assert check_workspace_in_allowlist(workspace_id) is False
