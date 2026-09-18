"""#1551 — "may create" vs "may serve" pins for the XL-only re-map.

Resources, connectors and public features are XL-only to *create*. Objects
that already exist on M/L keep working end to end, so the serve paths must
not consult the feature flag and the per-workspace effective caps they rely
on must keep their M/L values. These tests pin that split at the source and
at the ``Workspace.effective_*`` layer; the route-level "existing object keeps
working" cases live next to the routes (``tests/api``, ``tests/services``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from config.plan_tiers import PLAN_ORDER, has_feature
from models.auth import Workspace

_SRC = Path(__file__).resolve().parents[2] / "src"

# Anything that would turn a serve/secret path into a plan-feature gate.
_FEATURE_GATE_TOKENS = (
    "has_feature",
    "FEATURE_MIN_PLANS",
    "get_required_plan_for_feature",
    "feature_denied_message",
    "check_feature_access",
    "allows_shared_contexts",
)


def _source(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Secret store: available on every tier, no runtime plan gate at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    ["mcp_server/tools/secrets.py", "api/routes/secrets.py"],
)
def test_secret_store_call_sites_never_consult_the_plan(rel: str) -> None:
    src = _source(rel)
    for token in (*_FEATURE_GATE_TOKENS, "plan_name", "get_plan_tier"):
        assert token not in src, f"{rel} consults the plan via {token!r}"


def test_secret_store_is_on_every_tier() -> None:
    assert all(has_feature(plan, "secret_store") for plan in PLAN_ORDER)


# ---------------------------------------------------------------------------
# Serve paths for existing public contexts stay feature-flag-free
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    ["api/routes/public_search.py", "api/middleware/rate_limit.py"],
)
def test_public_serve_paths_do_not_gate_on_the_feature_flag(rel: str) -> None:
    """An existing L public context keeps answering: the serve path sizes its
    buckets from the tier's numeric caps only, never from ``public_contexts``."""
    src = _source(rel)
    for token in _FEATURE_GATE_TOKENS:
        assert token not in src, f"{rel} gates serving via {token!r}"
    # The numeric caps ARE what the serve path reads.
    assert re.search(r"public_calls_per_day|bound_public_calls_per_minute", src)


# ---------------------------------------------------------------------------
# Effective caps existing M/L objects rely on are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "tokens", "connectors", "public_per_day"),
    [("basic", 3, 3, 0), ("pro", 30, 10, 1000), ("promax", 150, 50, 5000)],
)
def test_effective_caps_keep_serving_existing_objects(
    plan: str, tokens: int, connectors: int, public_per_day: int
) -> None:
    ws = Workspace(plan_name=plan)
    assert ws.effective_max_resource_tokens == tokens
    assert ws.effective_max_connectors == connectors
    assert ws.effective_public_calls_per_day == public_per_day


def test_pro_workspace_without_the_feature_still_serves_public_traffic() -> None:
    """The bound-key bucket on an existing L public context is still 100/min."""
    ws = Workspace(plan_name="pro")
    assert not has_feature(ws.plan_name, "public_contexts")
    assert ws.effective_public_calls_per_day == 1000
    assert ws._plan_tier.bound_public_calls_per_minute == 100
