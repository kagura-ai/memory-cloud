"""Coverage-focused tests for QuotaService (DB-backed).

The companion suite ``test_quota_service.py`` already covers, with mocks,
``check_member_quota``, ``check_workspace_creation_allowed`` (cap + lock
policy). This file targets the remaining UNCOVERED paths against a real
``db_session``:

- ``check_memory_quota`` — empty / under-limit / at-limit / not-found / raise
- ``check_feature_access`` — granted / denied / unknown-feature fallback /
  not-found / raise
- ``check_context_creation_allowed`` — under / at-limit / addon bonus /
  not-found / raise
- ``count_mcp_calls_today`` — zero / today-only counting
- ``check_mcp_rate_limit`` — allowed / exceeded / not-found raises
- ``get_quota_status`` — not-found / no-members / with-members / warning /
  exceeded thresholds

Issue #149 / #238 / #229.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from config.plan_tiers import get_plan_tier
from models.auth import (
    Context,
    UsageStats,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from services import quota_service as qs
from services.quota_service import QuotaService
from utils.datetime import utcnow
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError

# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


async def _make_workspace(db, plan_name="free", **kwargs):
    """Insert and return a Workspace row with a unique id."""
    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        owner_user_id=kwargs.pop("owner_user_id", f"owner-{uuid4().hex[:8]}"),
        plan_name=plan_name,
        **kwargs,
    )
    db.add(ws)
    await db.flush()
    return ws


async def _add_member(db, workspace_id, user_id, role="member"):
    member = WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role)
    db.add(member)
    await db.flush()
    return member


async def _add_memory(db, user_id, workspace_id, **kwargs):
    mem = Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        summary="s",
        content="c",
        type="code",
        client="pytest",
        **kwargs,
    )
    db.add(mem)
    await db.flush()
    return mem


async def _add_context(db, workspace_id, name=None, deleted=False):
    ctx = Context(
        id=uuid4(),
        workspace_id=workspace_id,
        name=name or f"ctx{uuid4().hex[:8]}",
        created_by="creator",
    )
    if deleted:
        ctx.deleted_at = utcnow()
    db.add(ctx)
    await db.flush()
    return ctx


async def _add_usage(db, workspace_id, user_id="u", method="MCP", on_date=None):
    today = utcnow().date()
    row = UsageStats(
        user_id=user_id,
        endpoint="/x",
        method=method,
        status_code=200,
        date=on_date or today,
        workspace_id=workspace_id,
    )
    db.add(row)
    await db.flush()
    return row


# ---------------------------------------------------------------------------
# check_memory_quota
# ---------------------------------------------------------------------------


class TestCheckMemoryQuota:
    """QuotaService.check_memory_quota — counts memories across members."""

    async def test_no_memories_returns_true(self, db_session):
        """Zero memories short-circuits to (True, None) without a quota lookup."""
        ws = await _make_workspace(db_session, "free")
        service = QuotaService(db_session)

        can_create, error = await service.check_memory_quota(ws.id)

        assert can_create is True
        assert error is None

    async def test_under_limit_returns_true(self, db_session):
        """A few memories well under the free 1000 limit → allowed."""
        ws = await _make_workspace(db_session, "free")
        user_id = f"member-{uuid4().hex[:8]}"
        await _add_member(db_session, ws.id, user_id)
        await _add_memory(db_session, user_id, ws.id)
        await _add_memory(db_session, user_id, ws.id)
        service = QuotaService(db_session)

        can_create, error = await service.check_memory_quota(ws.id)

        assert can_create is True
        assert error is None

    async def test_orphaned_and_deleted_memories_excluded(self, db_session, monkeypatch):
        """NULL-workspace and soft-deleted rows are not counted toward usage.

        A control valid memory is inserted alongside and the limit is patched
        to 2, so a True result distinguishes "filters exclude the noise so
        only the 1 real row counts (1 < 2)" from a broken filter that would
        count all three (3 >= 2 → denied). This rules out the trivial case
        where True merely means "zero rows".
        """
        ws = await _make_workspace(db_session, "free")
        user_id = f"member-{uuid4().hex[:8]}"
        await _add_member(db_session, ws.id, user_id)
        # One real, counted memory (the control).
        await _add_memory(db_session, user_id, ws.id)
        # Orphaned (workspace_id NULL) — excluded by the isnot(None) filter.
        await _add_memory(db_session, user_id, None)
        # Soft-deleted — excluded by deleted_at.is_(None).
        await _add_memory(db_session, user_id, ws.id, deleted_at=utcnow())

        async def _limit_two(self, workspace_id):
            return {"memory_limit": 2}

        monkeypatch.setattr(qs.EffectiveQuotaService, "get_effective_quotas", _limit_two)
        service = QuotaService(db_session)

        # Only the 1 valid memory counts (1 < 2) → allowed. If the orphaned and
        # soft-deleted rows were counted, the total would be 3 >= 2 → denied.
        can_create, error = await service.check_memory_quota(ws.id)

        assert can_create is True
        assert error is None

    async def test_at_limit_returns_false(self, db_session, monkeypatch):
        """current_count >= effective limit → (False, message). Limit is
        patched tiny so we don't insert 1000 rows; counting stays real."""
        ws = await _make_workspace(db_session, "free")
        user_id = f"member-{uuid4().hex[:8]}"
        await _add_member(db_session, ws.id, user_id)
        await _add_memory(db_session, user_id, ws.id)
        await _add_memory(db_session, user_id, ws.id)

        async def _tiny_quota(self, workspace_id):
            return {"memory_limit": 2}

        monkeypatch.setattr(qs.EffectiveQuotaService, "get_effective_quotas", _tiny_quota)
        service = QuotaService(db_session)

        can_create, error = await service.check_memory_quota(ws.id)

        assert can_create is False
        assert error is not None
        assert "Memory quota exceeded" in error
        assert "Limit: 2" in error
        assert "free" in error

    async def test_at_limit_raises_when_requested(self, db_session, monkeypatch):
        """raise_on_exceeded=True converts the over-limit result to a raise."""
        ws = await _make_workspace(db_session, "free")
        user_id = f"member-{uuid4().hex[:8]}"
        await _add_member(db_session, ws.id, user_id)
        await _add_memory(db_session, user_id, ws.id)

        async def _tiny_quota(self, workspace_id):
            return {"memory_limit": 1}

        monkeypatch.setattr(qs.EffectiveQuotaService, "get_effective_quotas", _tiny_quota)
        service = QuotaService(db_session)

        with pytest.raises(QuotaExceededError) as exc:
            await service.check_memory_quota(ws.id, raise_on_exceeded=True)

        assert "Memory quota exceeded" in str(exc.value)

    async def test_workspace_not_found_returns_false(self, db_session):
        """Missing workspace → (False, 'not found')."""
        service = QuotaService(db_session)
        missing = uuid4()

        can_create, error = await service.check_memory_quota(missing)

        assert can_create is False
        assert "not found" in error

    async def test_workspace_not_found_raises(self, db_session):
        """Missing workspace + raise_on_exceeded=True → QuotaExceededError."""
        service = QuotaService(db_session)

        with pytest.raises(QuotaExceededError):
            await service.check_memory_quota(uuid4(), raise_on_exceeded=True)


# ---------------------------------------------------------------------------
# check_feature_access
# ---------------------------------------------------------------------------


class TestCheckFeatureAccess:
    """QuotaService.check_feature_access — plan-tier feature gating."""

    async def test_feature_granted(self, db_session):
        """'oauth' is in the free plan's feature set → granted."""
        ws = await _make_workspace(db_session, "free")
        service = QuotaService(db_session)

        has_access, error = await service.check_feature_access(ws.id, "oauth")

        assert has_access is True
        assert error is None

    async def test_feature_denied_surfaces_required_plan(self, db_session):
        """'team_invitations' requires Pro; free workspace is denied and the
        message names the Pro display name from the registry."""
        ws = await _make_workspace(db_session, "free")
        service = QuotaService(db_session)
        pro_display = get_plan_tier("pro").display_name

        has_access, error = await service.check_feature_access(ws.id, "team_invitations")

        assert has_access is False
        assert "team_invitations" in error
        assert "free" in error
        assert pro_display in error

    async def test_unknown_feature_falls_back_to_higher(self, db_session):
        """An unknown feature is not in any plan → denied; the required-plan
        lookup raises ValueError and the message falls back to 'higher'."""
        ws = await _make_workspace(db_session, "free")
        service = QuotaService(db_session)

        has_access, error = await service.check_feature_access(
            ws.id, "definitely_not_a_real_feature"
        )

        assert has_access is False
        assert "higher" in error

    async def test_feature_denied_raises_when_requested(self, db_session):
        """raise_on_denied=True → FeatureNotAvailableError."""
        ws = await _make_workspace(db_session, "free")
        service = QuotaService(db_session)

        with pytest.raises(FeatureNotAvailableError) as exc:
            await service.check_feature_access(ws.id, "team_invitations", raise_on_denied=True)

        assert "team_invitations" in str(exc.value)

    async def test_feature_workspace_not_found(self, db_session):
        """Missing workspace → (False, 'not found')."""
        service = QuotaService(db_session)

        has_access, error = await service.check_feature_access(uuid4(), "oauth")

        assert has_access is False
        assert "not found" in error

    async def test_feature_workspace_not_found_raises(self, db_session):
        """Missing workspace + raise_on_denied=True → FeatureNotAvailableError."""
        service = QuotaService(db_session)

        with pytest.raises(FeatureNotAvailableError):
            await service.check_feature_access(uuid4(), "oauth", raise_on_denied=True)


# ---------------------------------------------------------------------------
# check_context_creation_allowed
# ---------------------------------------------------------------------------


class TestCheckContextCreationAllowed:
    """QuotaService.check_context_creation_allowed — per-plan context cap."""

    async def test_under_limit_allowed(self, db_session):
        """Free plan allows 1 context; with 0 existing → allowed."""
        ws = await _make_workspace(db_session, "free")
        service = QuotaService(db_session)

        can_create, error = await service.check_context_creation_allowed(ws.id)

        assert can_create is True
        assert error is None

    async def test_at_limit_denied(self, db_session):
        """Free plan max=1; one existing context → denied with message."""
        ws = await _make_workspace(db_session, "free")
        await _add_context(db_session, ws.id)
        service = QuotaService(db_session)

        can_create, error = await service.check_context_creation_allowed(ws.id)

        assert can_create is False
        assert "Context limit reached" in error
        assert "1 context" in error

    async def test_soft_deleted_context_not_counted(self, db_session):
        """A soft-deleted context does not consume the cap → still allowed."""
        ws = await _make_workspace(db_session, "free")
        await _add_context(db_session, ws.id, deleted=True)
        service = QuotaService(db_session)

        can_create, error = await service.check_context_creation_allowed(ws.id)

        assert can_create is True
        assert error is None

    async def test_addon_context_bonus_expands_limit(self, db_session):
        """addon_context_bonus adds to plan base: free(1)+1 = 2 allowed,
        so one existing context is still under cap."""
        ws = await _make_workspace(db_session, "free", addon_context_bonus=1)
        await _add_context(db_session, ws.id)
        service = QuotaService(db_session)

        can_create, error = await service.check_context_creation_allowed(ws.id)

        assert can_create is True
        assert error is None

    async def test_at_limit_raises_when_requested(self, db_session):
        """raise_on_denied=True at the cap → QuotaExceededError."""
        ws = await _make_workspace(db_session, "free")
        await _add_context(db_session, ws.id)
        service = QuotaService(db_session)

        with pytest.raises(QuotaExceededError) as exc:
            await service.check_context_creation_allowed(ws.id, raise_on_denied=True)

        assert "Context limit reached" in str(exc.value)

    async def test_context_workspace_not_found(self, db_session):
        """Missing workspace → (False, 'not found')."""
        service = QuotaService(db_session)

        can_create, error = await service.check_context_creation_allowed(uuid4())

        assert can_create is False
        assert "not found" in error

    async def test_context_workspace_not_found_raises(self, db_session):
        """Missing workspace + raise_on_denied=True → QuotaExceededError."""
        service = QuotaService(db_session)

        with pytest.raises(QuotaExceededError):
            await service.check_context_creation_allowed(uuid4(), raise_on_denied=True)


# ---------------------------------------------------------------------------
# count_mcp_calls_today
# ---------------------------------------------------------------------------


class TestCountMcpCallsToday:
    """QuotaService.count_mcp_calls_today — today-only MCP usage count."""

    async def test_zero_when_no_usage(self, db_session):
        """No usage rows → 0."""
        ws = await _make_workspace(db_session, "free")
        service = QuotaService(db_session)

        assert await service.count_mcp_calls_today(ws.id) == 0

    async def test_counts_only_today_mcp(self, db_session):
        """Counts only today's method=='MCP' rows for this workspace —
        yesterday's MCP rows and today's non-MCP rows are excluded."""
        ws = await _make_workspace(db_session, "free")
        yesterday = utcnow().date() - timedelta(days=1)
        await _add_usage(db_session, ws.id, method="MCP")
        await _add_usage(db_session, ws.id, method="MCP")
        await _add_usage(db_session, ws.id, method="REST")  # wrong method
        await _add_usage(db_session, ws.id, method="MCP", on_date=yesterday)  # wrong day
        service = QuotaService(db_session)

        assert await service.count_mcp_calls_today(ws.id) == 2


# ---------------------------------------------------------------------------
# check_mcp_rate_limit
# ---------------------------------------------------------------------------


class TestCheckMcpRateLimit:
    """QuotaService.check_mcp_rate_limit — daily MCP cap enforcement."""

    async def test_allowed_under_limit(self, db_session):
        """Under the daily MCP limit → (True, used, limit)."""
        ws = await _make_workspace(db_session, "free")
        await _add_usage(db_session, ws.id, method="MCP")
        service = QuotaService(db_session)

        allowed, used, limit = await service.check_mcp_rate_limit(ws.id)

        assert allowed is True
        assert used == 1
        assert limit == get_plan_tier("free").mcp_calls_per_day

    async def test_exceeded_at_limit(self, db_session, monkeypatch):
        """used_today >= daily_limit → (False, used, limit). The effective
        limit is forced to 1 via the workspace property so we only insert
        a single usage row."""
        ws = await _make_workspace(db_session, "free")
        await _add_usage(db_session, ws.id, method="MCP")

        # Patch the workspace property to a tiny limit (property is read by
        # the service after fetching the workspace).
        monkeypatch.setattr(
            type(ws),
            "effective_mcp_calls_per_day",
            property(lambda self: 1),
        )
        service = QuotaService(db_session)

        allowed, used, limit = await service.check_mcp_rate_limit(ws.id)

        assert allowed is False
        assert used == 1
        assert limit == 1

    async def test_not_found_raises_value_error(self, db_session):
        """Missing workspace → ValueError (NOT QuotaExceededError here)."""
        service = QuotaService(db_session)

        with pytest.raises(ValueError) as exc:
            await service.check_mcp_rate_limit(uuid4())

        assert "not found" in str(exc.value)


# ---------------------------------------------------------------------------
# get_quota_status
# ---------------------------------------------------------------------------


class TestGetQuotaStatus:
    """QuotaService.get_quota_status — aggregate usage + feature snapshot."""

    async def test_not_found_returns_empty(self, db_session):
        """Missing workspace → {} sentinel."""
        service = QuotaService(db_session)

        assert await service.get_quota_status(uuid4()) == {}

    async def test_no_members_zero_memory(self, db_session):
        """No members → memory_count branch is the empty-member path (0)."""
        ws = await _make_workspace(db_session, "free")
        service = QuotaService(db_session)

        status = await service.get_quota_status(ws.id)

        assert status["memory"]["current"] == 0
        assert status["memory"]["percentage"] == 0
        assert status["memory"]["warning"] is False
        assert status["memory"]["exceeded"] is False
        # Free plan features snapshot.
        assert status["features"]["oauth"] is True
        assert status["features"]["reranking"] is False
        assert status["plan"]["name"] == "free"
        assert status["plan"]["display_name"] == get_plan_tier("free").display_name

    async def test_with_members_counts_memories(self, db_session):
        """With members and memories, current count is the live JOIN count
        and percentage is computed against the effective limit."""
        ws = await _make_workspace(db_session, "free")
        user_id = f"member-{uuid4().hex[:8]}"
        await _add_member(db_session, ws.id, user_id)
        await _add_memory(db_session, user_id, ws.id)
        await _add_memory(db_session, user_id, ws.id)
        # excluded rows
        await _add_memory(db_session, user_id, None)  # orphaned
        await _add_memory(db_session, user_id, ws.id, deleted_at=utcnow())  # deleted
        service = QuotaService(db_session)

        status = await service.get_quota_status(ws.id)

        assert status["memory"]["current"] == 2
        limit = get_plan_tier("free").memory_limit
        assert status["memory"]["limit"] == limit
        expected_pct = round(2 / limit * 100, 2)
        assert status["memory"]["percentage"] == expected_pct

    async def test_pro_features_reflected(self, db_session):
        """A Pro workspace's feature snapshot reports reranking (Basic+) and
        oauth (available on all tiers) as enabled."""
        ws = await _make_workspace(db_session, "pro")
        service = QuotaService(db_session)

        status = await service.get_quota_status(ws.id)

        assert status["features"]["reranking"] is True
        assert status["features"]["oauth"] is True

    async def test_warning_and_exceeded_thresholds(self, db_session, monkeypatch):
        """percentage >= 80 sets warning; >= 100 sets exceeded. We force a
        tiny effective_memory_limit so two memories cross 100%."""
        ws = await _make_workspace(db_session, "free")
        user_id = f"member-{uuid4().hex[:8]}"
        await _add_member(db_session, ws.id, user_id)
        await _add_memory(db_session, user_id, ws.id)
        await _add_memory(db_session, user_id, ws.id)

        monkeypatch.setattr(
            type(ws),
            "effective_memory_limit",
            property(lambda self: 2),
        )
        service = QuotaService(db_session)

        status = await service.get_quota_status(ws.id)

        assert status["memory"]["current"] == 2
        assert status["memory"]["percentage"] == 100.0
        assert status["memory"]["warning"] is True
        assert status["memory"]["exceeded"] is True
