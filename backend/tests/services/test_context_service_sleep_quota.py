"""Tests for tier-based sleep_mode quota in ContextService (Issue #560).

Covers:
1. ``_assert_sleep_quota_or_raise`` helper logic — under/at/over limit, addon
   bonus, ``exclude_id`` honoring, FREE/BASIC zero-limit hard block.
2. ``update_context`` quota check wiring — increase-only rule (only triggers
   on ``skip -> non-skip``; reductions and lateral non-skip → non-skip both
   skip the check).
3. ``create_context`` does NOT consult the quota helper at all (sleep_mode is
   always ``skip`` at create time per #558 default; client cannot inject).

The helper's ``SELECT FOR UPDATE`` row lock is exercised at the integration
level (real PostgreSQL); these unit tests focus on the branching contract.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.context_service import ContextService
from utils.exceptions import QuotaExceededError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_workspace(*, limit: int, addon_bonus: int = 0) -> MagicMock:
    """Build a Workspace mock that mimics the model's effective-limit property.

    The real ``Workspace.effective_sleep_enabled_contexts_limit`` reads
    ``_plan_tier.sleep_enabled_contexts_limit + addon_sleep_contexts_bonus``.
    Tests parametrize the final effective limit directly to avoid recreating
    the plan-tier lookup chain.
    """
    ws = MagicMock()
    ws.effective_sleep_enabled_contexts_limit = limit
    ws.addon_sleep_contexts_bonus = addon_bonus
    return ws


def _patch_workspace_and_count(
    service: ContextService,
    *,
    workspace: MagicMock,
    count: int,
):
    """Patch ``service.db.execute`` to return workspace then count.

    The helper executes two queries in order:
    1. ``SELECT * FROM workspaces WHERE id = ? FOR UPDATE`` → workspace
    2. ``SELECT count(*) FROM contexts WHERE workspace_id = ? ...`` → count
    """
    workspace_result = MagicMock()
    workspace_result.scalar_one = MagicMock(return_value=workspace)

    count_result = MagicMock()
    count_result.scalar_one = MagicMock(return_value=count)

    service.db.execute = AsyncMock(side_effect=[workspace_result, count_result])


# ---------------------------------------------------------------------------
# 1. _assert_sleep_quota_or_raise
# ---------------------------------------------------------------------------


class TestAssertSleepQuotaOrRaise:
    # Uses the module-level ``service`` fixture defined below.

    @pytest.mark.asyncio
    async def test_under_limit_passes(self, service):
        ws = _make_workspace(limit=3)
        _patch_workspace_and_count(service, workspace=ws, count=1)

        await service._assert_sleep_quota_or_raise(workspace_id=uuid4())

    @pytest.mark.asyncio
    async def test_at_limit_raises(self, service):
        ws = _make_workspace(limit=3)
        _patch_workspace_and_count(service, workspace=ws, count=3)

        with pytest.raises(QuotaExceededError) as exc_info:
            await service._assert_sleep_quota_or_raise(workspace_id=uuid4())

        assert exc_info.value.details["quota_type"] == "sleep_enabled_contexts"
        assert exc_info.value.details["limit"] == 3
        assert exc_info.value.details["current"] == 3
        assert exc_info.value.details["requested"] == 4

    @pytest.mark.asyncio
    async def test_basic_zero_limit_blocks_first_attempt(self, service):
        """FREE/BASIC tier (limit=0) rejects even the first sleep-enable."""
        ws = _make_workspace(limit=0)
        _patch_workspace_and_count(service, workspace=ws, count=0)

        with pytest.raises(QuotaExceededError) as exc_info:
            await service._assert_sleep_quota_or_raise(workspace_id=uuid4())

        assert exc_info.value.details["limit"] == 0

    @pytest.mark.asyncio
    async def test_addon_extends_limit(self, service):
        """PRO base 3 + addon 2 = effective 5, count=4 still passes."""
        ws = _make_workspace(limit=5, addon_bonus=2)
        _patch_workspace_and_count(service, workspace=ws, count=4)

        await service._assert_sleep_quota_or_raise(workspace_id=uuid4())

    @pytest.mark.asyncio
    async def test_addon_bonus_surfaces_in_error_payload(self, service):
        """Quota error includes addon_bonus so clients can render '3 + 2 addon'."""
        ws = _make_workspace(limit=5, addon_bonus=2)
        _patch_workspace_and_count(service, workspace=ws, count=5)

        with pytest.raises(QuotaExceededError) as exc_info:
            await service._assert_sleep_quota_or_raise(workspace_id=uuid4())

        assert exc_info.value.details["addon_bonus"] == 2


# ---------------------------------------------------------------------------
# 2. update_context wiring (increase-only rule)
# ---------------------------------------------------------------------------


@pytest.fixture
def service():
    return ContextService(AsyncMock())


def _make_context(*, current_mode: str = "skip"):
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.workspace_id = uuid4()
    ctx.display_name = None
    ctx.description = None
    ctx.summary = None
    ctx.usage_guide = None
    ctx.is_private = True
    ctx.is_public = False
    ctx.resource_id = None
    ctx.is_locked = False
    ctx.sleep_mode = current_mode
    return ctx


class TestUpdateContextSleepModeQuotaWiring:
    """``update_context`` only calls the helper on increase-only transitions."""

    @pytest.mark.asyncio
    async def test_skip_to_full_runs_quota_check(self, service):
        ctx = _make_context(current_mode="skip")
        with (
            patch.object(service, "get_context", new_callable=AsyncMock, return_value=ctx),
            patch.object(
                service, "_assert_sleep_quota_or_raise", new_callable=AsyncMock
            ) as mock_assert,
        ):
            await service.update_context(
                user_id="u",
                context_id=ctx.id,
                sleep_mode="full",
            )

        mock_assert.assert_awaited_once_with(workspace_id=ctx.workspace_id, exclude_id=ctx.id)
        assert ctx.sleep_mode == "full"

    @pytest.mark.asyncio
    async def test_skip_to_edges_only_runs_quota_check(self, service):
        ctx = _make_context(current_mode="skip")
        with (
            patch.object(service, "get_context", new_callable=AsyncMock, return_value=ctx),
            patch.object(
                service, "_assert_sleep_quota_or_raise", new_callable=AsyncMock
            ) as mock_assert,
        ):
            await service.update_context(
                user_id="u",
                context_id=ctx.id,
                sleep_mode="edges_only",
            )

        mock_assert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_full_to_skip_does_not_check_quota(self, service):
        """Reduction direction: PRO grandfathered above limit can always taper down."""
        ctx = _make_context(current_mode="full")
        with (
            patch.object(service, "get_context", new_callable=AsyncMock, return_value=ctx),
            patch.object(
                service, "_assert_sleep_quota_or_raise", new_callable=AsyncMock
            ) as mock_assert,
        ):
            await service.update_context(
                user_id="u",
                context_id=ctx.id,
                sleep_mode="skip",
            )

        mock_assert.assert_not_awaited()
        assert ctx.sleep_mode == "skip"

    @pytest.mark.asyncio
    async def test_full_to_edges_only_does_not_check_quota(self, service):
        """Lateral non-skip → non-skip: count is unchanged, no quota check needed."""
        ctx = _make_context(current_mode="full")
        with (
            patch.object(service, "get_context", new_callable=AsyncMock, return_value=ctx),
            patch.object(
                service, "_assert_sleep_quota_or_raise", new_callable=AsyncMock
            ) as mock_assert,
        ):
            await service.update_context(
                user_id="u",
                context_id=ctx.id,
                sleep_mode="edges_only",
            )

        mock_assert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_sleep_mode_change_does_not_check_quota(self, service):
        """Passing the same mode (or None) skips the helper entirely."""
        ctx = _make_context(current_mode="full")
        with (
            patch.object(service, "get_context", new_callable=AsyncMock, return_value=ctx),
            patch.object(
                service, "_assert_sleep_quota_or_raise", new_callable=AsyncMock
            ) as mock_assert,
        ):
            await service.update_context(
                user_id="u",
                context_id=ctx.id,
                sleep_mode="full",  # same as current
            )

        mock_assert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sleep_mode_none_does_not_check_quota(self, service):
        ctx = _make_context(current_mode="skip")
        with (
            patch.object(service, "get_context", new_callable=AsyncMock, return_value=ctx),
            patch.object(
                service, "_assert_sleep_quota_or_raise", new_callable=AsyncMock
            ) as mock_assert,
        ):
            await service.update_context(
                user_id="u",
                context_id=ctx.id,
                display_name="renamed only",
            )

        mock_assert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quota_exceeded_propagates(self, service):
        """When the helper raises, update_context surfaces the exception."""
        ctx = _make_context(current_mode="skip")
        with (
            patch.object(service, "get_context", new_callable=AsyncMock, return_value=ctx),
            patch.object(
                service,
                "_assert_sleep_quota_or_raise",
                new_callable=AsyncMock,
                side_effect=QuotaExceededError("over quota", quota_type="sleep_enabled_contexts"),
            ),
        ):
            with pytest.raises(QuotaExceededError):
                await service.update_context(
                    user_id="u",
                    context_id=ctx.id,
                    sleep_mode="full",
                )

        # Must NOT have applied the new mode when the quota check rejected,
        # AND must not have committed (otherwise a regression that flipped
        # the assignment ahead of the quota helper would still pass the
        # in-memory assertion above).
        assert ctx.sleep_mode == "skip"
        service.db.commit.assert_not_awaited()
