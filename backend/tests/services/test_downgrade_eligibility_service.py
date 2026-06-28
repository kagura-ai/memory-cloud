"""Unit tests for DowngradeEligibilityService (#1123).

The eligibility MATH (usage vs target-tier effective limits, addons kept) is the
load-bearing logic and is tested here with no DB: ``_evaluate_tier`` is a pure
function of (workspace addon bonuses, usage counts, target PlanTier). The
workspace-scoped COUNT queries are pinned separately in
tests/integration/test_downgrade_eligibility_db.py.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from config.plan_tiers import PLAN_TIERS
from services.downgrade_eligibility_service import (
    DowngradeEligibilityService,
    WorkspaceUsage,
)

FREE = PLAN_TIERS["free"]
BASIC = PLAN_TIERS["basic"]
PRO = PLAN_TIERS["pro"]


def _ws(plan_name="pro", *, member=0, context=0, memory=0):
    """A minimal workspace stand-in carrying the 3 addon bonuses the math reads."""
    return types.SimpleNamespace(
        id=uuid4(),
        plan_name=plan_name,
        addon_member_bonus=member,
        addon_context_bonus=context,
        addon_memory_bonus=memory,
    )


def _usage(*, members=0, contexts=0, shared=0, memories=0, tokens=0):
    return WorkspaceUsage(
        members=members,
        contexts=contexts,
        shared_contexts=shared,
        memories=memories,
        resource_tokens=tokens,
    )


def _svc():
    return DowngradeEligibilityService(db=MagicMock())


def _by_dim(result):
    return {b.dimension: b for b in result.blockers}


# --------------------------------------------------------------------------- #
# _evaluate_tier — the eligibility matrix
# --------------------------------------------------------------------------- #


def test_all_within_target_is_eligible():
    # PRO workspace, tiny usage → fits BASIC with no blockers.
    result = _svc()._evaluate_tier(
        _ws(), _usage(members=1, contexts=1, memories=10, tokens=0, shared=0), BASIC
    )
    assert result.target_plan == "basic"
    assert result.eligible is True
    assert result.blockers == []


def test_members_over_target_blocks():
    result = _svc()._evaluate_tier(_ws(), _usage(members=5), FREE)  # free limit = 1
    assert result.eligible is False
    b = _by_dim(result)["members"]
    assert (b.usage, b.limit, b.overage, b.cleanup) == (5, 1, 4, "remove_members")


def test_contexts_over_target_blocks():
    result = _svc()._evaluate_tier(_ws(), _usage(contexts=25), BASIC)  # basic limit = 3
    b = _by_dim(result)["contexts"]
    assert (b.usage, b.limit, b.overage, b.cleanup) == (25, 3, 22, "delete_contexts")


def test_memories_over_target_blocks():
    result = _svc()._evaluate_tier(_ws(), _usage(memories=2000), FREE)  # free memory_limit = 1000
    b = _by_dim(result)["memories"]
    assert (b.usage, b.limit, b.overage, b.cleanup) == (2000, 1000, 1000, "delete_memories")


def test_resource_tokens_over_target_blocks():
    # tier-fixed cap (no addon): basic = 3.
    result = _svc()._evaluate_tier(_ws(), _usage(tokens=5), BASIC)
    b = _by_dim(result)["resource_tokens"]
    assert (b.usage, b.limit, b.overage, b.cleanup) == (5, 3, 2, "revoke_resource_tokens")


def test_shared_contexts_block_when_target_disallows():
    result = _svc()._evaluate_tier(_ws(), _usage(shared=2), FREE)  # free disallows shared
    b = _by_dim(result)["shared_contexts"]
    assert (b.usage, b.limit, b.overage, b.cleanup) == (2, 0, 2, "unshare_contexts")


def test_shared_contexts_no_block_when_zero():
    result = _svc()._evaluate_tier(_ws(), _usage(shared=0), FREE)
    assert "shared_contexts" not in _by_dim(result)


def test_addons_are_kept_and_raise_the_target_effective_limit():
    # +5 context addon on BASIC (base 3) → effective 8. 8 fits, 9 does not.
    ws = _ws(context=5)
    assert _svc()._evaluate_tier(ws, _usage(contexts=8), BASIC).eligible is True
    over = _svc()._evaluate_tier(ws, _usage(contexts=9), BASIC)
    b = _by_dim(over)["contexts"]
    assert (b.limit, b.overage) == (8, 1)  # base(3) + addon(5) = 8


def test_multiple_blockers_collected():
    result = _svc()._evaluate_tier(_ws(), _usage(members=5, contexts=25, shared=2), FREE)
    assert result.eligible is False
    assert set(_by_dim(result)) == {"members", "contexts", "shared_contexts"}


def test_at_limit_is_not_over():
    # usage == effective limit is allowed (strict ">" blocks).
    result = _svc()._evaluate_tier(_ws(), _usage(contexts=3), BASIC)  # basic limit = 3
    assert result.eligible is True


# --------------------------------------------------------------------------- #
# evaluate — which tiers are downgrade targets
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_evaluate_free_workspace_has_no_targets():
    # Lowest tier → no downgrade targets, and current_usage is never queried.
    svc = _svc()
    svc.current_usage = AsyncMock()
    assert await svc.evaluate(_ws(plan_name="free")) == []
    svc.current_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluate_basic_targets_free_only():
    svc = _svc()
    svc.current_usage = AsyncMock(return_value=_usage())
    targets = await svc.evaluate(_ws(plan_name="basic"))
    assert [t.target_plan for t in targets] == ["free"]


@pytest.mark.asyncio
async def test_evaluate_pro_targets_free_then_basic_in_order():
    svc = _svc()
    svc.current_usage = AsyncMock(return_value=_usage())
    targets = await svc.evaluate(_ws(plan_name="pro"))
    assert [t.target_plan for t in targets] == ["free", "basic"]


@pytest.mark.asyncio
async def test_evaluate_unknown_plan_fails_closed_to_no_targets():
    svc = _svc()
    svc.current_usage = AsyncMock()
    assert await svc.evaluate(_ws(plan_name="enterprise")) == []
    svc.current_usage.assert_not_awaited()
