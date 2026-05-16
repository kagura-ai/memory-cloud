"""Tests for QuotaService.

Issue #229: Team member limit (10 members max for Pro plan)
Issue #661: Plan-based owned-workspace cap
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.quota_service import QuotaService
from utils.exceptions import QuotaExceededError


class TestQuotaServiceMemberQuota:
    """Test QuotaService.check_member_quota() for member limit enforcement."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        """Create QuotaService instance."""
        return QuotaService(mock_db)

    @pytest.fixture
    def workspace_id(self):
        """Generate test workspace ID."""
        return uuid4()

    def _make_workspace(self, workspace_id, plan_name, **kwargs):
        """Build a workspace mock with addon bonuses pre-set.

        Issue #570 made ``EffectiveQuotaService.get_effective_quotas`` pure-read,
        so the historical "skip the addon-recalc branch by setting one bonus
        non-zero" trick is no longer load-bearing — the branch is gone.
        ``addon_member_bonus`` stays 0 so the plan's native ``max_members`` limit
        is used unchanged. The 4-execute call count for ``check_member_quota``
        below is still correct because there is no recalc-driven extra SELECT
        either before or after #570.
        """
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = plan_name
        workspace.memory_limit = kwargs.get("memory_limit", 1000)
        workspace.addon_memory_bonus = 1
        workspace.addon_mcp_quota_bonus = 0
        workspace.addon_rest_quota_bonus = 0
        workspace.addon_public_quota_bonus = 0
        workspace.addon_member_bonus = 0
        workspace.addon_context_bonus = 0
        for k, v in kwargs.items():
            setattr(workspace, k, v)
        # Simulate @property effective quotas (plan tier lookup not available in mock)
        from config.plan_tiers import get_plan_tier

        tier = get_plan_tier(plan_name)
        workspace.effective_memory_limit = workspace.memory_limit + workspace.addon_memory_bonus
        workspace.effective_mcp_calls_per_day = (
            tier.mcp_calls_per_day + workspace.addon_mcp_quota_bonus
        )
        workspace.effective_max_members = (
            tier.max_members_per_workspace + workspace.addon_member_bonus
        )
        workspace.effective_max_contexts = (
            tier.max_contexts_per_workspace + workspace.addon_context_bonus
        )
        return workspace

    def _make_side_effects(self, workspace, member_count, pending_count):
        """Return the 4-element side_effect list for mock_db.execute.

        Call order inside check_member_quota:
          1. select(Workspace)                     – quota_service
          2. select(count(WorkspaceMember.id))     – member count
          3. select(count(WorkspaceInvitation.id)) – pending count
          4. select(Workspace)                     – EffectiveQuotaService
        """
        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        member_count_result = MagicMock()
        member_count_result.scalar = MagicMock(return_value=member_count)

        pending_count_result = MagicMock()
        pending_count_result.scalar = MagicMock(return_value=pending_count)

        effective_workspace_result = MagicMock()
        effective_workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        return [
            workspace_result,
            member_count_result,
            pending_count_result,
            effective_workspace_result,
        ]

    @pytest.mark.asyncio
    async def test_check_member_quota_within_limit(self, service, mock_db, workspace_id):
        """Test quota check when within limit (Pro plan, 5/10 members)."""
        workspace = self._make_workspace(workspace_id, "pro", memory_limit=100000)
        mock_db.execute.side_effect = self._make_side_effects(workspace, 5, 0)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is True
        assert error is None

    @pytest.mark.asyncio
    async def test_check_member_quota_at_limit(self, service, mock_db, workspace_id):
        """Test quota check when at limit (Pro plan, 10/10 members)."""
        workspace = self._make_workspace(workspace_id, "pro")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 10, 0)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is False
        assert error is not None
        assert "Member limit reached" in error
        assert "10 seats" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_with_pending_invitations(
        self, service, mock_db, workspace_id
    ):
        """Test quota includes pending invitations (8 members + 2 pending = 10/10)."""
        workspace = self._make_workspace(workspace_id, "pro")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 8, 2)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is False
        assert "Current members: 8, Pending invitations: 2" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_basic_plan(self, service, mock_db, workspace_id):
        """Test quota for Basic plan (1 member limit, owner only)."""
        workspace = self._make_workspace(workspace_id, "basic")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 1, 0)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is False
        assert "1 seats" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_raise_on_exceeded(self, service, mock_db, workspace_id):
        """Test that QuotaExceededError is raised when raise_on_exceeded=True."""
        workspace = self._make_workspace(workspace_id, "pro")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 10, 0)

        with pytest.raises(QuotaExceededError) as exc_info:
            await service.check_member_quota(workspace_id, raise_on_exceeded=True)

        assert "Member limit reached" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_check_member_quota_workspace_not_found(self, service, mock_db, workspace_id):
        """Test error when workspace doesn't exist."""
        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute.side_effect = [workspace_result]

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is False
        assert "not found" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_workspace_not_found_raises(
        self, service, mock_db, workspace_id
    ):
        """Test that QuotaExceededError is raised when workspace not found and raise_on_exceeded=True."""
        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute.side_effect = [workspace_result]

        with pytest.raises(QuotaExceededError):
            await service.check_member_quota(workspace_id, raise_on_exceeded=True)

    @pytest.mark.asyncio
    async def test_check_member_quota_free_plan(self, service, mock_db, workspace_id):
        """Test quota for Free plan (1 member limit)."""
        workspace = self._make_workspace(workspace_id, "free")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 1, 0)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is False
        assert "1 seats" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_pro_available_seats(self, service, mock_db, workspace_id):
        """Test quota check returns success when seats available (Pro plan, 3/10)."""
        workspace = self._make_workspace(workspace_id, "pro")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 2, 1)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is True
        assert error is None


class TestQuotaServiceWorkspaceCreationCap:
    """Test QuotaService.check_workspace_creation_allowed() — #674 sub-A, #675.

    Post-#675 slot-pivot semantics: the cap is ``1 + workspace_slot_bonus``
    instead of the prior plan-tier-derived cap. The helper
    ``get_user_workspace_cap_summary`` returns ``(owned_count, slot_bonus)``
    via a single JOIN; the mock arms one ``execute()`` call returning a
    Row with attribute access for those two values.

    Settings are patched at ``config.settings.get_settings`` (the
    method's lazy-import lookup target).
    """

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return QuotaService(mock_db)

    def _patch_settings(self, enforce: bool):
        """Patch ``enforce_workspace_cap`` for the call duration.

        Patches at the definition site (``config.settings.get_settings``)
        because the gate's lazy ``from config.settings import get_settings``
        looks the name up on the source module at call time. A patch on
        ``services.quota_service.get_settings`` would fail with
        ``AttributeError`` — no module-level binding exists there.
        """
        mock_settings = MagicMock()
        mock_settings.enforce_workspace_cap = enforce
        return patch("config.settings.get_settings", return_value=mock_settings)

    def _arm_db(self, mock_db, owned_count: int, slot_bonus: int):
        """Configure mock_db.execute for the single SELECT inside
        ``get_user_workspace_cap_summary``.

        The helper calls ``result.one_or_none()`` and reads
        ``row.owned_count`` and ``row.workspace_slot_bonus``.
        """
        row = MagicMock()
        row.owned_count = owned_count
        row.workspace_slot_bonus = slot_bonus
        result = MagicMock()
        result.one_or_none = MagicMock(return_value=row)
        mock_db.execute.return_value = result

    # ------------------------------------------------------------------
    # Base cap (bonus=0 → cap=1)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_zero_owned_no_bonus_can_create(self, service, mock_db):
        """0 owned, 0 bonus → cap=1, 0 < 1 → allowed."""
        self._arm_db(mock_db, owned_count=0, slot_bonus=0)
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is True
        assert error is None

    @pytest.mark.asyncio
    async def test_one_owned_no_bonus_denied_when_enforced(self, service, mock_db):
        """1 owned, 0 bonus → cap=1, 1 >= 1 → denied with flag=True."""
        self._arm_db(mock_db, owned_count=1, slot_bonus=0)
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is False
        assert error is not None
        assert "Workspace limit reached" in error
        assert "1 workspace" in error  # owned_count + cap surfaced
        # Error message is tier-neutral — must not reference plan names.
        assert "FREE" not in error
        assert "BASIC" not in error
        assert "PRO" not in error

    @pytest.mark.asyncio
    async def test_one_owned_no_bonus_allowed_when_flag_off(self, service, mock_db):
        """1 owned, 0 bonus → over cap but flag=False → log-only allow."""
        self._arm_db(mock_db, owned_count=1, slot_bonus=0)
        with self._patch_settings(enforce=False):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is True
        assert error is None

    # ------------------------------------------------------------------
    # Bonus expands the cap
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_two_owned_bonus_two_can_create(self, service, mock_db):
        """2 owned, 2 bonus → cap=3, 2 < 3 → allowed."""
        self._arm_db(mock_db, owned_count=2, slot_bonus=2)
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is True
        assert error is None

    @pytest.mark.asyncio
    async def test_three_owned_bonus_two_denied(self, service, mock_db):
        """3 owned, 2 bonus → cap=3, 3 >= 3 → denied. Error surfaces 3 / 3."""
        self._arm_db(mock_db, owned_count=3, slot_bonus=2)
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is False
        assert error is not None
        assert "3 workspace" in error  # owned_count
        assert "cap: 3" in error  # cap = 1 + bonus

    # ------------------------------------------------------------------
    # Grandfather case (large existing ownership)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_grandfathered_five_owned_bonus_four_at_cap(self, service, mock_db):
        """Migration grandfather case: 5 owned, 4 bonus → cap=5, at cap, denied."""
        self._arm_db(mock_db, owned_count=5, slot_bonus=4)
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_admin_grant_zero_owned_three_bonus_can_create(self, service, mock_db):
        """Phase 1 admin pre-grant: 0 owned, 3 bonus → cap=4, allowed."""
        self._arm_db(mock_db, owned_count=0, slot_bonus=3)
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is True
        assert error is None

    # ------------------------------------------------------------------
    # raise_on_denied behaviour
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_raises_on_denied_when_enforced(self, service, mock_db):
        """raise_on_denied=True raises QuotaExceededError when flag=True and over cap."""
        self._arm_db(mock_db, owned_count=1, slot_bonus=0)
        with self._patch_settings(enforce=True):
            with pytest.raises(QuotaExceededError) as exc_info:
                await service.check_workspace_creation_allowed("user-1", raise_on_denied=True)
        assert "Workspace limit reached" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_does_not_raise_when_flag_off(self, service, mock_db):
        """raise_on_denied=True does NOT raise when flag=False — log-only mode."""
        self._arm_db(mock_db, owned_count=1, slot_bonus=0)
        with self._patch_settings(enforce=False):
            can_create, error = await service.check_workspace_creation_allowed(
                "user-1", raise_on_denied=True
            )
        assert can_create is True
        assert error is None

    # ------------------------------------------------------------------
    # Lock-error policy (#677 sub-C, PR #686 review follow-up)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_lock_error(sqlstate: str) -> Exception:
        """Build a SQLAlchemy DBAPIError carrying a ``sqlstate`` attribute.

        The production code reads ``exc.orig.sqlstate`` / ``exc.orig.pgcode``
        — both forms appear on different asyncpg/psycopg2 versions — to
        identify SQLSTATE 55P03 (``lock_not_available``). A minimal
        ``MagicMock`` orig with ``sqlstate`` set satisfies the read.
        """
        from sqlalchemy.exc import DBAPIError

        orig = MagicMock()
        orig.sqlstate = sqlstate
        orig.pgcode = sqlstate
        return DBAPIError("stmt", {}, orig)

    @pytest.mark.asyncio
    async def test_lock_timeout_enforce_true_denies(self, service, mock_db, monkeypatch):
        """SQLSTATE 55P03 + enforce=True → fail-closed (deny + rollback)."""
        from services import quota_service as qs

        async def _raise_lock_timeout(self_, user_id):
            raise self._make_lock_error("55P03")

        monkeypatch.setattr(qs.QuotaService, "_acquire_workspace_create_lock", _raise_lock_timeout)
        mock_db.rollback = AsyncMock()
        warning_mock = MagicMock()
        monkeypatch.setattr(qs.logger, "warning", warning_mock)

        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")

        assert can_create is False
        assert error is not None
        assert "temporarily unavailable" in error.lower()
        mock_db.rollback.assert_awaited_once()
        event_calls = [
            c
            for c in warning_mock.call_args_list
            if c.args and c.args[0] == "workspace_create_lock_failed"
        ]
        assert len(event_calls) == 1
        assert event_calls[0].kwargs.get("reason") == "lock_timeout"
        assert event_calls[0].kwargs.get("sqlstate") == "55P03"
        assert event_calls[0].kwargs.get("enforced") is True

    @pytest.mark.asyncio
    async def test_lock_timeout_enforce_false_allows(self, service, mock_db, monkeypatch):
        """SQLSTATE 55P03 + enforce=False → fail-open (allow + log, no cap read)."""
        from services import quota_service as qs

        async def _raise_lock_timeout(self_, user_id):
            raise self._make_lock_error("55P03")

        monkeypatch.setattr(qs.QuotaService, "_acquire_workspace_create_lock", _raise_lock_timeout)
        mock_db.rollback = AsyncMock()
        warning_mock = MagicMock()
        monkeypatch.setattr(qs.logger, "warning", warning_mock)

        with self._patch_settings(enforce=False):
            can_create, error = await service.check_workspace_creation_allowed("user-1")

        assert can_create is True
        assert error is None
        mock_db.rollback.assert_awaited_once()
        event_calls = [
            c
            for c in warning_mock.call_args_list
            if c.args and c.args[0] == "workspace_create_lock_failed"
        ]
        assert len(event_calls) == 1
        assert event_calls[0].kwargs.get("reason") == "lock_timeout"
        assert event_calls[0].kwargs.get("enforced") is False

    @pytest.mark.asyncio
    async def test_non_timeout_dbapi_error_reason_is_lock_error(
        self, service, mock_db, monkeypatch
    ):
        """Non-55P03 DBAPIError → ``reason='lock_error'`` (distinct from lock_timeout)."""
        from services import quota_service as qs

        async def _raise_other(self_, user_id):
            raise self._make_lock_error("08006")  # connection_failure

        monkeypatch.setattr(qs.QuotaService, "_acquire_workspace_create_lock", _raise_other)
        mock_db.rollback = AsyncMock()
        warning_mock = MagicMock()
        monkeypatch.setattr(qs.logger, "warning", warning_mock)

        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")

        assert can_create is False
        event_calls = [
            c
            for c in warning_mock.call_args_list
            if c.args and c.args[0] == "workspace_create_lock_failed"
        ]
        assert len(event_calls) == 1
        assert event_calls[0].kwargs.get("reason") == "lock_error"
        assert event_calls[0].kwargs.get("sqlstate") == "08006"

    @pytest.mark.asyncio
    async def test_lock_error_raise_on_denied_chains_cause(self, service, mock_db, monkeypatch):
        """raise_on_denied=True + enforce=True + lock error → QuotaExceededError chained from DBAPIError."""
        from services import quota_service as qs

        original = self._make_lock_error("55P03")

        async def _raise_lock_timeout(self_, user_id):
            raise original

        monkeypatch.setattr(qs.QuotaService, "_acquire_workspace_create_lock", _raise_lock_timeout)
        mock_db.rollback = AsyncMock()

        with self._patch_settings(enforce=True):
            with pytest.raises(QuotaExceededError) as exc_info:
                await service.check_workspace_creation_allowed("user-1", raise_on_denied=True)

        assert "temporarily unavailable" in str(exc_info.value).lower()
        assert exc_info.value.__cause__ is original
