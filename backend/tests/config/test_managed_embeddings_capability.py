"""Tests for the ``managed_embeddings`` plan capability (Issue #1030).

Paid tiers (basic/pro = M/L) carry ``managed_embeddings`` so they may embed on
the platform key without BYOK; Free (S) does not (BYOK or self-host Ollama).
"""

from config.plan_tiers import get_required_plan_for_feature, has_feature


def test_paid_tiers_have_managed_embeddings():
    assert has_feature("basic", "managed_embeddings") is True
    assert has_feature("pro", "managed_embeddings") is True


def test_free_tier_lacks_managed_embeddings():
    assert has_feature("free", "managed_embeddings") is False


def test_managed_embeddings_min_plan_is_basic():
    assert get_required_plan_for_feature("managed_embeddings") == "basic"
