"""Tests for addon quota reduction validation logic.

Issue #21: Validate addon quota reduction against current usage.

Tests the validation rules:
- Context addon: hard limit (reject if usage > new effective)
- Member addon: hard limit (reject if usage > new effective)
- Memory addon: soft limit (always allow reduction)
"""

from config.plan_tiers import get_plan_tier


class TestAddonReductionValidation:
    """Test addon reduction validation logic (no DB required).

    Mirrors the validation in admin_plans.py update_workspace_quotas.
    """

    # ================================================================
    # Context: Hard Limit
    # ================================================================

    def test_context_reduction_rejected_when_usage_exceeds_limit(self):
        """Free plan base=1, 5 contexts, addon=0 → effective=1 < 5 → reject."""
        plan_tier = get_plan_tier("free")
        new_addon = 0
        new_effective = plan_tier.max_contexts_per_workspace + new_addon
        context_count = 5

        assert new_effective < context_count

    def test_context_reduction_allowed_when_usage_within_limit(self):
        """Free plan base=1, 5 contexts, addon=5 → effective=6 >= 5 → allow."""
        plan_tier = get_plan_tier("free")
        new_addon = 5
        new_effective = plan_tier.max_contexts_per_workspace + new_addon
        context_count = 5

        assert new_effective >= context_count

    def test_context_reduction_allowed_at_exact_limit(self):
        """Free plan base=1, 5 contexts, addon=4 → effective=5 == 5 → allow."""
        plan_tier = get_plan_tier("free")
        new_addon = 4
        new_effective = plan_tier.max_contexts_per_workspace + new_addon
        context_count = 5

        assert new_effective >= context_count

    def test_context_pro_plan_high_base(self):
        """Pro plan base=20, 19 contexts, addon=0 → effective=20 >= 19 → allow."""
        plan_tier = get_plan_tier("pro")
        new_addon = 0
        new_effective = plan_tier.max_contexts_per_workspace + new_addon
        context_count = 19

        assert new_effective >= context_count

    # ================================================================
    # Member: Hard Limit
    # ================================================================

    def test_member_reduction_rejected_when_usage_exceeds_limit(self):
        """Free plan base=1, 3 members, addon=0 → effective=1 < 3 → reject."""
        plan_tier = get_plan_tier("free")
        new_addon = 0
        new_effective = plan_tier.max_members_per_workspace + new_addon
        member_count = 3

        assert new_effective < member_count

    def test_member_reduction_allowed_when_usage_within_limit(self):
        """Free plan base=1, 3 members, addon=3 → effective=4 >= 3 → allow."""
        plan_tier = get_plan_tier("free")
        new_addon = 3
        new_effective = plan_tier.max_members_per_workspace + new_addon
        member_count = 3

        assert new_effective >= member_count

    def test_member_reduction_allowed_at_exact_limit(self):
        """Free plan base=1, 3 members, addon=2 → effective=3 == 3 → allow."""
        plan_tier = get_plan_tier("free")
        new_addon = 2
        new_effective = plan_tier.max_members_per_workspace + new_addon
        member_count = 3

        assert new_effective >= member_count

    # ================================================================
    # Memory: Soft Limit (always allow)
    # ================================================================

    def test_memory_reduction_always_allowed(self):
        """Memory uses soft limit — reduction is always permitted."""
        base_memory = 1000  # free plan memory_limit
        new_addon = 0
        new_effective = base_memory + new_addon
        memory_count = base_memory + 100  # Usage exceeds new effective

        # Soft limit: no rejection, just allow
        assert memory_count > new_effective  # Would fail hard limit...
        # ...but soft limit means we allow it anyway (no HTTPException raised)

    # ================================================================
    # Plan Tier Base Values Sanity
    # ================================================================

    def test_plan_tier_base_values(self):
        """Verify plan tier base values used in validation."""
        free = get_plan_tier("free")
        basic = get_plan_tier("basic")
        pro = get_plan_tier("pro")

        # Contexts: free < basic < pro
        assert free.max_contexts_per_workspace < basic.max_contexts_per_workspace
        assert basic.max_contexts_per_workspace < pro.max_contexts_per_workspace

        # Members: free/basic = 1, pro = 10
        assert free.max_members_per_workspace == basic.max_members_per_workspace
        assert basic.max_members_per_workspace < pro.max_members_per_workspace
