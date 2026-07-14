"""Coverage tests for ``auth.analysis_gates`` (Issue #496).

Targets the read-only primitives that compose the Memory Broadlistening
access gates, exercised against a real ``db_session`` plus the
settings-driven kill switch:

- ``check_workspace_in_allowlist`` — pure, settings-driven membership
  check (re-exported from ``auth.analysis_allowlist``). Empty allowlist
  → False platform-wide; populated → only listed UUIDs True;
  case-insensitive on both sides.
- ``_get_user_timezone`` — fetches ``User.timezone``, defaults to UTC
  when the user row is missing or the column is empty.
- ``check_memory_analysis_quota`` — counts ``memory_analyses`` rows in
  the caller's day window (caller timezone), raises 429 at the cap and
  passes below it. Cancelled rows count toward the cap; a missing
  workspace surfaces a 500 ``ConfigurationError``; an exotic timezone
  string does not 500 the gate.

The composed FastAPI ``Depends`` chains
(``require_memory_analysis_access`` / ``require_memory_analysis_read`` /
``check_memory_analysis_access_mcp``) are already locked structurally by
``tests/api/test_analyses_quota_precedence.py``; this module focuses on
the underlying primitives with a real DB so the count / day-window /
timezone branches are covered for real.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException

from auth.analysis_gates import (
    _get_user_timezone,
    check_memory_analysis_access_mcp,
    check_memory_analysis_quota,
    check_workspace_in_allowlist,
    require_memory_analysis_access,
    require_memory_analysis_read,
)
from services.analysis.query_service import day_window_utc
from utils.exceptions import (
    AuthorizationError,
    ConfigurationError,
    FeatureNotAvailableError,
    QuotaExceededError,
)

# ---------------------------------------------------------------------------
# Fixtures — build the minimal real rows the gate primitives SELECT.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pro_workspace(db_session):
    """A PRO-plan workspace (analysis_runs_per_day base == 3)."""
    from models.auth import Workspace

    ws = Workspace(
        id=uuid4(),
        name="Pro WS",
        owner_user_id=f"owner-{uuid4()}",
        plan_name="pro",
    )
    db_session.add(ws)
    await db_session.flush()
    return ws


@pytest_asyncio.fixture
async def free_workspace(db_session):
    """A FREE-plan workspace (analysis_runs_per_day base == 0)."""
    from models.auth import Workspace

    ws = Workspace(
        id=uuid4(),
        name="Free WS",
        owner_user_id=f"owner-{uuid4()}",
        plan_name="free",
    )
    db_session.add(ws)
    await db_session.flush()
    return ws


@pytest_asyncio.fixture
async def pricing(db_session):
    """A stub ``llm_pricing`` row so ``MemoryAnalysis.model_id`` FK is satisfied."""
    from datetime import datetime

    from models.llm_pricing import LLMPricing

    row = LLMPricing(
        provider="openai",
        model="gpt-5-nano",
        unit_type="input_tokens",
        price_per_unit=0.20,
        effective_from=datetime(2026, 1, 1),
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest_asyncio.fixture
async def context_in(db_session):
    """Factory: create a Context belonging to a workspace."""
    from models.auth import Context

    async def _make(workspace_id: UUID) -> UUID:
        ctx = Context(
            id=uuid4(),
            workspace_id=workspace_id,
            name=f"ctx_{uuid4().hex[:8]}",
            display_name="Ctx",
            created_by="creator",
            is_private=False,
        )
        db_session.add(ctx)
        await db_session.flush()
        return ctx.id

    return _make


async def _make_owner(db_session, workspace_id: UUID, *, timezone: str = "UTC") -> str:
    """Create a User + owner WorkspaceMember for ``workspace_id``.

    Returns the ``user_id`` so ``PermissionService.check_workspace_owner``
    (real, no mocks) passes for the composed-gate tests.
    """
    from auth.workspace_roles import WorkspaceRole
    from models.auth import User, WorkspaceMember

    uid = f"owner-{uuid4()}"
    user = User(email=f"{uid}@example.com", user_id=uid, timezone=timezone)
    member = WorkspaceMember(
        workspace_id=workspace_id,
        user_id=uid,
        role=WorkspaceRole.OWNER,
    )
    db_session.add_all([user, member])
    await db_session.flush()
    return uid


async def _make_run(
    db_session,
    *,
    workspace_id: UUID,
    context_id: UUID,
    pricing_row,
    status: str,
    started_at,
):
    """Insert one ``memory_analyses`` row with an explicit ``started_at``."""
    from models.analysis import MemoryAnalysis

    run = MemoryAnalysis(
        id=uuid4(),
        workspace_id=workspace_id,
        context_id=context_id,
        triggered_by="test_user",
        status=status,
        started_at=started_at,
        finished_at=None if status == "running" else started_at + timedelta(seconds=5),
        model_id=pricing_row.id,
        model_snapshot={"model": "gpt-5-nano"},
        embedding_model="text-embedding-3-small",
        params={},
        input_count=3,
        cost_estimated_cents=1,
        cost_actual_cents=1 if status == "succeeded" else None,
        paid_by="byok",
    )
    db_session.add(run)
    await db_session.flush()
    return run


# ===========================================================================
# check_workspace_in_allowlist — pure, settings-driven kill switch
# ===========================================================================


class TestCheckWorkspaceInAllowlist:
    """The allowlist kill-switch primitive (re-exported from analysis_allowlist)."""

    def _set_allowlist(self, monkeypatch, value: str) -> None:
        from config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "analysis_enabled_workspace_ids", value, raising=False)

    def test_empty_allowlist_denies_everyone(self, monkeypatch):
        """Empty ANALYSIS_ENABLED_WORKSPACE_IDS → feature OFF platform-wide."""
        self._set_allowlist(monkeypatch, "")
        assert check_workspace_in_allowlist(uuid4()) is False

    def test_whitespace_only_allowlist_denies(self, monkeypatch):
        """A whitespace-only value parses to an empty list → False."""
        self._set_allowlist(monkeypatch, "   ,  , ")
        assert check_workspace_in_allowlist(uuid4()) is False

    def test_listed_workspace_allowed(self, monkeypatch):
        """A UUID present in the allowlist returns True."""
        wid = uuid4()
        self._set_allowlist(monkeypatch, f"{wid},{uuid4()}")
        assert check_workspace_in_allowlist(wid) is True

    def test_unlisted_workspace_denied(self, monkeypatch):
        """A populated allowlist denies a UUID not in it."""
        self._set_allowlist(monkeypatch, f"{uuid4()},{uuid4()}")
        assert check_workspace_in_allowlist(uuid4()) is False

    def test_case_insensitive_match(self, monkeypatch):
        """Upper-case env values still match — both sides are lower-cased."""
        wid = uuid4()
        self._set_allowlist(monkeypatch, str(wid).upper())
        # Passing the canonical (lower-case) UUID still matches.
        assert check_workspace_in_allowlist(wid) is True

    def test_accepts_string_workspace_id(self, monkeypatch):
        """A string workspace_id (not UUID object) is accepted and matched."""
        wid = uuid4()
        self._set_allowlist(monkeypatch, str(wid))
        assert check_workspace_in_allowlist(str(wid)) is True


# ===========================================================================
# _get_user_timezone — User.timezone fetch with UTC fallback
# ===========================================================================


class TestGetUserTimezone:
    """Fetches the caller's timezone, defaulting to UTC."""

    async def test_returns_stored_timezone(self, db_session):
        """Returns the User.timezone value when the row exists."""
        from models.auth import User

        uid = f"tz-user-{uuid4()}"
        user = User(
            email=f"{uid}@example.com",
            user_id=uid,
            timezone="Asia/Tokyo",
        )
        db_session.add(user)
        await db_session.flush()

        tz = await _get_user_timezone(db_session, uid)
        assert tz == "Asia/Tokyo"

    async def test_missing_user_defaults_to_utc(self, db_session):
        """No matching user row → 'UTC' fallback."""
        tz = await _get_user_timezone(db_session, f"nonexistent-{uuid4()}")
        assert tz == "UTC"

    async def test_empty_timezone_defaults_to_utc(self, db_session):
        """An empty-string timezone falls back to 'UTC' (``tz or 'UTC'``)."""
        from models.auth import User

        uid = f"tz-empty-{uuid4()}"
        user = User(
            email=f"{uid}@example.com",
            user_id=uid,
            timezone="",
        )
        db_session.add(user)
        await db_session.flush()

        tz = await _get_user_timezone(db_session, uid)
        assert tz == "UTC"


# ===========================================================================
# check_memory_analysis_quota — read-only count, 429 at the cap
# ===========================================================================


class TestCheckMemoryAnalysisQuota:
    """Counts today's runs in the caller's timezone; raises 429 at the cap."""

    async def test_passes_below_cap(self, db_session, pro_workspace, pricing, context_in):
        """Below the PRO cap (3): no exception raised."""
        ctx = await context_in(pro_workspace.id)
        day_start, _ = day_window_utc("UTC")
        for _ in range(2):  # 2 < 3
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=day_start + timedelta(hours=1),
            )

        # No raise == pass.
        await check_memory_analysis_quota(
            db_session, workspace_id=pro_workspace.id, user_timezone="UTC"
        )

    async def test_raises_429_at_cap(self, db_session, pro_workspace, pricing, context_in):
        """At the PRO cap (3 used): QuotaExceededError with QUOTA-001 detail."""
        ctx = await context_in(pro_workspace.id)
        day_start, _ = day_window_utc("UTC")
        for _ in range(3):  # 3 >= 3 → exhausted
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=day_start + timedelta(hours=2),
            )

        with pytest.raises(QuotaExceededError) as exc:
            await check_memory_analysis_quota(
                db_session, workspace_id=pro_workspace.id, user_timezone="UTC"
            )
        err = exc.value
        assert err.status_code == 429
        assert err.error_code == "QUOTA-001"
        assert err.details["used_today"] == 3
        assert err.details["limit_today"] == 3
        assert err.details["remaining_today"] == 0
        assert err.details["quota_type"] == "memory_analysis"
        # resets_at is an ISO string for the caller's next midnight.
        assert isinstance(err.details["resets_at"], str)
        assert err.details["addon_bonus"] == 0

    async def test_cancelled_rows_count_toward_quota(
        self, db_session, pro_workspace, pricing, context_in
    ):
        """``cancelled`` runs count toward the cap (conservative counting)."""
        ctx = await context_in(pro_workspace.id)
        day_start, _ = day_window_utc("UTC")
        # 1 succeeded + 2 cancelled == 3 == cap → must raise.
        await _make_run(
            db_session,
            workspace_id=pro_workspace.id,
            context_id=ctx,
            pricing_row=pricing,
            status="succeeded",
            started_at=day_start + timedelta(hours=1),
        )
        for _ in range(2):
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="cancelled",
                started_at=day_start + timedelta(hours=1),
            )

        with pytest.raises(QuotaExceededError) as exc:
            await check_memory_analysis_quota(
                db_session, workspace_id=pro_workspace.id, user_timezone="UTC"
            )
        assert exc.value.details["used_today"] == 3

    async def test_rows_outside_day_window_not_counted(
        self, db_session, pro_workspace, pricing, context_in
    ):
        """Runs started yesterday do not count against today's quota."""
        ctx = await context_in(pro_workspace.id)
        day_start, _ = day_window_utc("UTC")
        # 4 runs but all BEFORE today's window → used_today == 0 → pass.
        for _ in range(4):
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=day_start - timedelta(hours=2),
            )

        # No raise: every row falls outside [day_start, day_end).
        await check_memory_analysis_quota(
            db_session, workspace_id=pro_workspace.id, user_timezone="UTC"
        )

    async def test_other_workspace_rows_not_counted(
        self, db_session, pro_workspace, free_workspace, pricing, context_in
    ):
        """Quota count is scoped to the workspace — foreign rows ignored."""
        # Put 3 runs in the FREE workspace's context today; they must NOT
        # count against the PRO workspace's quota.
        other_ctx = await context_in(free_workspace.id)
        day_start, _ = day_window_utc("UTC")
        for _ in range(3):
            await _make_run(
                db_session,
                workspace_id=free_workspace.id,
                context_id=other_ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=day_start + timedelta(hours=1),
            )

        # PRO workspace has zero rows today → passes despite the foreign 3.
        await check_memory_analysis_quota(
            db_session, workspace_id=pro_workspace.id, user_timezone="UTC"
        )

    async def test_missing_workspace_raises_configuration_error(self, db_session):
        """A workspace_id with no row → 500 ConfigurationError (CFG-001)."""
        with pytest.raises(ConfigurationError) as exc:
            await check_memory_analysis_quota(db_session, workspace_id=uuid4(), user_timezone="UTC")
        assert exc.value.status_code == 500
        assert exc.value.error_code == "CFG-001"

    async def test_exotic_timezone_does_not_500_and_formats_reset(
        self, db_session, pro_workspace, pricing, context_in
    ):
        """An invalid tz string falls back to UTC for resets_at (no 500).

        ``day_window_utc`` already coerces an exotic tz to UTC for the
        count window; the 429 branch's own ``try/except ZoneInfo`` guards
        the ``resets_at`` formatting. Drive the cap with an exotic tz to
        exercise that defensive branch and assert a clean 429 instead of a
        ZoneInfo crash.
        """
        ctx = await context_in(pro_workspace.id)
        # Use the UTC window for inserts since the exotic tz coerces to UTC.
        day_start, _ = day_window_utc("Mars/Phobos")
        for _ in range(3):
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=day_start + timedelta(hours=1),
            )

        with pytest.raises(QuotaExceededError) as exc:
            await check_memory_analysis_quota(
                db_session,
                workspace_id=pro_workspace.id,
                user_timezone="Mars/Phobos",
            )
        assert exc.value.status_code == 429
        assert isinstance(exc.value.details["resets_at"], str)

    async def test_timezone_aware_window_for_tokyo_caller(
        self, db_session, pro_workspace, pricing, context_in
    ):
        """A valid non-UTC tz drives a valid resets_at + count window.

        Inserting at the Asia/Tokyo day-start (converted to naive UTC)
        guarantees the rows fall inside the Tokyo window, exercising the
        non-UTC ZoneInfo path in the resets_at formatting branch.
        """
        ctx = await context_in(pro_workspace.id)
        tz = "Asia/Tokyo"
        day_start, day_end = day_window_utc(tz)
        # Place rows safely inside the window (just after local midnight).
        inside = day_start + timedelta(minutes=30)
        # Guard the fixture assumption: window must be non-empty.
        assert inside < day_end
        for _ in range(3):
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=inside,
            )

        with pytest.raises(QuotaExceededError) as exc:
            await check_memory_analysis_quota(
                db_session, workspace_id=pro_workspace.id, user_timezone=tz
            )
        # resets_at carries the +09:00 offset for a Tokyo caller.
        assert "+09:00" in exc.value.details["resets_at"]

    async def test_addon_bonus_raises_cap(self, db_session, pro_workspace, pricing, context_in):
        """A workspace addon bonus lifts the effective cap (base 3 + 2 = 5)."""
        # Set an addon bonus so the effective cap is 5; 3 runs must pass.
        pro_workspace.addon_analysis_bonus = 2
        db_session.add(pro_workspace)
        await db_session.flush()

        ctx = await context_in(pro_workspace.id)
        day_start, _ = day_window_utc("UTC")
        for _ in range(3):  # 3 < 5
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=day_start + timedelta(hours=1),
            )

        # No raise: 3 used < effective cap 5.
        await check_memory_analysis_quota(
            db_session, workspace_id=pro_workspace.id, user_timezone="UTC"
        )

    async def test_addon_bonus_surfaced_in_429_detail(
        self, db_session, pro_workspace, pricing, context_in
    ):
        """When over the addon-raised cap, addon_bonus is echoed in the 429."""
        pro_workspace.addon_analysis_bonus = 2  # effective cap 5
        db_session.add(pro_workspace)
        await db_session.flush()

        ctx = await context_in(pro_workspace.id)
        day_start, _ = day_window_utc("UTC")
        for _ in range(5):  # 5 >= 5 → exhausted
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=day_start + timedelta(hours=1),
            )

        with pytest.raises(QuotaExceededError) as exc:
            await check_memory_analysis_quota(
                db_session, workspace_id=pro_workspace.id, user_timezone="UTC"
            )
        assert exc.value.details["limit_today"] == 5
        assert exc.value.details["addon_bonus"] == 2


# ===========================================================================
# require_memory_analysis_access — full REST gate chain (real DB)
# ===========================================================================


class TestRequireMemoryAnalysisAccess:
    """The POST/DELETE 4-gate chain, driven against real rows."""

    async def test_missing_user_id_raises_401(self, db_session):
        """No ``user_id`` in the auth dict → 401 HTTPException."""
        with pytest.raises(HTTPException) as exc:
            await require_memory_analysis_access(user={}, db=db_session)
        assert exc.value.status_code == 401

    async def test_missing_workspace_raises_400(self, db_session):
        """``user_id`` present but no ``current_workspace_id`` → 400."""
        with pytest.raises(HTTPException) as exc:
            await require_memory_analysis_access(user={"user_id": "u1"}, db=db_session)
        assert exc.value.status_code == 400

    async def test_full_grant_returns_tuple(
        self, db_session, pro_workspace, pricing, context_in, monkeypatch
    ):
        """Owner + PRO + under quota + allowlisted → returns the access tuple."""
        owner = await _make_owner(db_session, pro_workspace.id, timezone="Asia/Tokyo")
        # Allowlist this workspace.
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            str(pro_workspace.id),
            raising=False,
        )
        user = {"user_id": owner, "current_workspace_id": pro_workspace.id}

        user_id, workspace_id, tz = await require_memory_analysis_access(user=user, db=db_session)
        assert user_id == owner
        assert workspace_id == pro_workspace.id
        assert tz == "Asia/Tokyo"

    async def test_non_owner_denied_403(self, db_session, pro_workspace, monkeypatch):
        """A user with no owner membership → AuthorizationError (403)."""
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            str(pro_workspace.id),
            raising=False,
        )
        user = {
            "user_id": f"stranger-{uuid4()}",
            "current_workspace_id": pro_workspace.id,
        }
        with pytest.raises(AuthorizationError) as exc:
            await require_memory_analysis_access(user=user, db=db_session)
        assert exc.value.status_code == 403

    async def test_free_plan_denied_feature_403(self, db_session, free_workspace):
        """Owner of a FREE workspace fails gate 3 (Pro tier) → 403 FEAT-001."""
        owner = await _make_owner(db_session, free_workspace.id)
        user = {"user_id": owner, "current_workspace_id": free_workspace.id}
        with pytest.raises(FeatureNotAvailableError) as exc:
            await require_memory_analysis_access(user=user, db=db_session)
        assert exc.value.status_code == 403
        assert exc.value.error_code == "FEAT-001"

    async def test_quota_exhausted_429(
        self, db_session, pro_workspace, pricing, context_in, monkeypatch
    ):
        """Owner + PRO + quota at cap → QuotaExceededError (429) from gate 4."""
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            str(pro_workspace.id),
            raising=False,
        )
        owner = await _make_owner(db_session, pro_workspace.id)
        ctx = await context_in(pro_workspace.id)
        day_start, _ = day_window_utc("UTC")
        for _ in range(3):
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=day_start + timedelta(hours=1),
            )
        user = {"user_id": owner, "current_workspace_id": pro_workspace.id}
        with pytest.raises(QuotaExceededError) as exc:
            await require_memory_analysis_access(user=user, db=db_session)
        assert exc.value.status_code == 429

    async def test_allowlist_denied_after_quota_passes(
        self, db_session, pro_workspace, monkeypatch
    ):
        """Owner + PRO + under quota but NOT allowlisted → gate 5 403."""
        from config.settings import get_settings

        # Empty allowlist → kill switch active.
        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            "",
            raising=False,
        )
        owner = await _make_owner(db_session, pro_workspace.id)
        user = {"user_id": owner, "current_workspace_id": pro_workspace.id}
        with pytest.raises(FeatureNotAvailableError) as exc:
            await require_memory_analysis_access(user=user, db=db_session)
        assert exc.value.status_code == 403


# ===========================================================================
# require_memory_analysis_read — GET gate chain (gate 1+2+5 only)
# ===========================================================================


class TestRequireMemoryAnalysisRead:
    """Read gate: owner + allowlist, skipping Pro tier and quota."""

    async def test_missing_user_id_raises_401(self, db_session):
        """No ``user_id`` → 401."""
        with pytest.raises(HTTPException) as exc:
            await require_memory_analysis_read(user={}, db=db_session)
        assert exc.value.status_code == 401

    async def test_missing_workspace_raises_400(self, db_session):
        """No ``current_workspace_id`` → 400."""
        with pytest.raises(HTTPException) as exc:
            await require_memory_analysis_read(user={"user_id": "u1"}, db=db_session)
        assert exc.value.status_code == 400

    async def test_free_plan_owner_can_read(self, db_session, free_workspace, monkeypatch):
        """A FREE owner can still read (no Pro/quota gate) when allowlisted."""
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            str(free_workspace.id),
            raising=False,
        )
        owner = await _make_owner(db_session, free_workspace.id, timezone="UTC")
        user = {"user_id": owner, "current_workspace_id": free_workspace.id}
        user_id, workspace_id, tz = await require_memory_analysis_read(user=user, db=db_session)
        assert user_id == owner
        assert workspace_id == free_workspace.id
        assert tz == "UTC"

    async def test_non_owner_read_denied_403(self, db_session, free_workspace, monkeypatch):
        """Read gate still enforces owner membership → 403."""
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            str(free_workspace.id),
            raising=False,
        )
        user = {
            "user_id": f"stranger-{uuid4()}",
            "current_workspace_id": free_workspace.id,
        }
        with pytest.raises(AuthorizationError) as exc:
            await require_memory_analysis_read(user=user, db=db_session)
        assert exc.value.status_code == 403

    async def test_read_allowlist_denied_403(self, db_session, free_workspace, monkeypatch):
        """Owner but workspace removed from allowlist → read still 403."""
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            "",
            raising=False,
        )
        owner = await _make_owner(db_session, free_workspace.id)
        user = {"user_id": owner, "current_workspace_id": free_workspace.id}
        with pytest.raises(FeatureNotAvailableError) as exc:
            await require_memory_analysis_read(user=user, db=db_session)
        assert exc.value.status_code == 403


# ===========================================================================
# check_memory_analysis_access_mcp — MCP-side composition (no Depends)
# ===========================================================================


class TestCheckMemoryAnalysisAccessMcp:
    """MCP parity with the REST chain; ``require_quota`` toggles gates 3+4."""

    async def test_full_grant_returns_timezone(self, db_session, pro_workspace, monkeypatch):
        """Owner + PRO + under quota + allowlisted → returns caller timezone."""
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            str(pro_workspace.id),
            raising=False,
        )
        owner = await _make_owner(db_session, pro_workspace.id, timezone="Asia/Tokyo")
        tz = await check_memory_analysis_access_mcp(
            db_session,
            user_id=owner,
            workspace_id=pro_workspace.id,
            require_quota=True,
        )
        assert tz == "Asia/Tokyo"

    async def test_require_quota_false_skips_tier_and_quota(
        self, db_session, free_workspace, monkeypatch
    ):
        """``require_quota=False`` lets a FREE owner through (read-equivalent)."""
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            str(free_workspace.id),
            raising=False,
        )
        owner = await _make_owner(db_session, free_workspace.id, timezone="UTC")
        tz = await check_memory_analysis_access_mcp(
            db_session,
            user_id=owner,
            workspace_id=free_workspace.id,
            require_quota=False,
        )
        assert tz == "UTC"

    async def test_non_owner_denied(self, db_session, pro_workspace):
        """Non-owner → AuthorizationError, gates 3-5 never run."""
        with pytest.raises(AuthorizationError):
            await check_memory_analysis_access_mcp(
                db_session,
                user_id=f"stranger-{uuid4()}",
                workspace_id=pro_workspace.id,
                require_quota=True,
            )

    async def test_free_plan_feature_denied(self, db_session, free_workspace):
        """FREE owner with require_quota=True fails the Pro tier gate → 403."""
        owner = await _make_owner(db_session, free_workspace.id)
        with pytest.raises(FeatureNotAvailableError) as exc:
            await check_memory_analysis_access_mcp(
                db_session,
                user_id=owner,
                workspace_id=free_workspace.id,
                require_quota=True,
            )
        assert exc.value.status_code == 403

    async def test_quota_exhausted_429(
        self, db_session, pro_workspace, pricing, context_in, monkeypatch
    ):
        """Owner + PRO at cap → QuotaExceededError via the MCP path."""
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            str(pro_workspace.id),
            raising=False,
        )
        owner = await _make_owner(db_session, pro_workspace.id)
        ctx = await context_in(pro_workspace.id)
        day_start, _ = day_window_utc("UTC")
        for _ in range(3):
            await _make_run(
                db_session,
                workspace_id=pro_workspace.id,
                context_id=ctx,
                pricing_row=pricing,
                status="succeeded",
                started_at=day_start + timedelta(hours=1),
            )
        with pytest.raises(QuotaExceededError):
            await check_memory_analysis_access_mcp(
                db_session,
                user_id=owner,
                workspace_id=pro_workspace.id,
                require_quota=True,
            )

    async def test_allowlist_denied_with_quota_skipped(
        self, db_session, free_workspace, monkeypatch
    ):
        """require_quota=False but not allowlisted → gate 5 still 403."""
        from config.settings import get_settings

        monkeypatch.setattr(
            get_settings(),
            "analysis_enabled_workspace_ids",
            "",
            raising=False,
        )
        owner = await _make_owner(db_session, free_workspace.id)
        with pytest.raises(FeatureNotAvailableError) as exc:
            await check_memory_analysis_access_mcp(
                db_session,
                user_id=owner,
                workspace_id=free_workspace.id,
                require_quota=False,
            )
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# #1240 — the quota gate serializes concurrent evaluations per workspace
# ---------------------------------------------------------------------------


class TestQuotaAdvisoryLock:
    @pytest.mark.asyncio
    async def test_quota_check_acquires_advisory_xact_lock(self, db_session, pro_workspace):
        """#1240: the gate MUST hold a pg advisory xact lock through the
        quota COUNT — this is the whole fix for the concurrent-start
        quota over-admission race (the partial unique index only guards
        same-context duplicates, not the per-workspace daily cap).
        Deleting the lock statement makes this fail.
        """
        from sqlalchemy import text as sql_text

        from auth.analysis_gates import check_memory_analysis_quota

        before = int(
            (
                await db_session.execute(
                    sql_text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
                    )
                )
            ).scalar()
            or 0
        )
        await check_memory_analysis_quota(
            db_session,
            workspace_id=pro_workspace.id,
            user_timezone="UTC",
        )
        # The xact lock is held until this transaction ends, so it must
        # be visible from within the same (still-open) transaction.
        after = int(
            (
                await db_session.execute(
                    sql_text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
                    )
                )
            ).scalar()
            or 0
        )
        assert after > before, "quota gate did not acquire the advisory xact lock"
        await db_session.rollback()
