"""API tests for the workspace plan-tiers comparison-matrix endpoint (#1138).

Direct TestClient pattern (mirrors ``test_admin_plans_tiers``). The endpoint
returns process-global ``PLAN_TIERS`` config and never touches the DB, so the
``get_db`` override returns a MagicMock; the session auth dependency is
overridden with a stub user. Expected values are the ``config/plan_tiers.py``
defaults.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_session_auth
from db.base import get_db

# Two-segment path (like /workspaces/plans/available) so it isn't shadowed by
# the earlier GET /workspaces/{workspace_id} route (a one-segment /plan-tiers
# would 422 on the UUID parse).
ENDPOINT = "/api/v1/workspaces/plans/tiers"


def _mock_session_user() -> dict:
    return {"user_id": "session_runner", "email": "user@test.invalid"}


@pytest.fixture
def client():
    async def mock_session():
        return _mock_session_user()

    async def mock_db():
        yield MagicMock()

    app.dependency_overrides[require_session_auth] = mock_session
    app.dependency_overrides[get_db] = mock_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_returns_four_tiers_in_upgrade_order(client: TestClient) -> None:
    resp = client.get(ENDPOINT)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert [t["name"] for t in data] == ["free", "basic", "pro", "promax"]


def test_curated_numeric_limits_match_plan_tiers(client: TestClient) -> None:
    free, basic, pro, _promax = client.get(ENDPOINT).json()

    # #1551: connectors are XL-only to CREATE. The matrix is the creation
    # view, so non-XL rows read 0 (✗) even though the tier dataclass keeps
    # basic=3 / pro=10 as serve-only caps for existing connectors.
    assert (free["max_connectors"], basic["max_connectors"], pro["max_connectors"]) == (0, 0, 0)
    assert (
        free["analysis_runs_per_day"],
        basic["analysis_runs_per_day"],
        pro["analysis_runs_per_day"],
    ) == (0, 0, 3)
    assert (
        free["sleep_enabled_contexts_limit"],
        basic["sleep_enabled_contexts_limit"],
        pro["sleep_enabled_contexts_limit"],
    ) == (0, 0, 3)

    # Headline quotas.
    assert (free["max_contexts"], basic["max_contexts"], pro["max_contexts"]) == (1, 3, 20)
    assert (free["max_members"], basic["max_members"], pro["max_members"]) == (1, 1, 10)
    # #1550: owned-workspace cap a user on this tier gets (1 base + tier grant).
    assert (
        free["owned_workspaces"],
        basic["owned_workspaces"],
        pro["owned_workspaces"],
        _promax["owned_workspaces"],
    ) == (1, 1, 3, 20)
    assert (free["memory_limit"], basic["memory_limit"], pro["memory_limit"]) == (
        1000,
        10000,
        100000,
    )
    assert (
        free["rest_calls_per_day"],
        basic["rest_calls_per_day"],
        pro["rest_calls_per_day"],
    ) == (0, 1000, 5000)
    # #1551: same creation-view rule for public calls and resource tokens.
    assert (
        free["public_calls_per_day"],
        basic["public_calls_per_day"],
        pro["public_calls_per_day"],
    ) == (0, 0, 0)
    assert (
        free["max_resource_tokens"],
        basic["max_resource_tokens"],
        pro["max_resource_tokens"],
    ) == (0, 0, 0)


def test_matrix_zeroes_xl_only_rows_without_touching_the_tier_numbers(
    client: TestClient,
) -> None:
    """The "—" for M/L comes from the matrix builder, NOT from the dataclass:
    existing M/L tokens / connectors / public contexts still serve against
    the unchanged tier caps (block-new-only, #1551)."""
    from config.plan_tiers import get_plan_tier

    _free, basic, pro, promax = client.get(ENDPOINT).json()
    assert (basic["max_resource_tokens"], pro["max_resource_tokens"]) == (0, 0)
    assert (
        get_plan_tier("basic").max_resource_tokens,
        get_plan_tier("pro").max_resource_tokens,
    ) == (
        3,
        30,
    )
    assert get_plan_tier("pro").public_calls_per_day == 1000
    # XL carries the real creation numbers.
    assert (
        promax["max_resource_tokens"],
        promax["max_connectors"],
        promax["public_calls_per_day"],
    ) == (150, 50, 5000)


def test_xl_only_feature_booleans(client: TestClient) -> None:
    free, basic, pro, promax = client.get(ENDPOINT).json()
    for key in ("resources", "connectors", "public_contexts"):
        assert (free[key], basic[key], pro[key], promax[key]) == (False, False, False, True), key


def test_boolean_capabilities(client: TestClient) -> None:
    free, basic, pro, _promax = client.get(ENDPOINT).json()
    assert (free["reranking"], basic["reranking"], pro["reranking"]) == (False, True, True)
    assert (
        free["managed_embeddings"],
        basic["managed_embeddings"],
        pro["managed_embeddings"],
    ) == (False, True, True)
    assert (
        free["shared_contexts"],
        basic["shared_contexts"],
        pro["shared_contexts"],
    ) == (False, False, True)
    assert (
        free["team_invitations"],
        basic["team_invitations"],
        pro["team_invitations"],
    ) == (False, False, True)
    # secret_store (Volt, #1128) is available on every tier.
    assert (
        free["secret_store"],
        basic["secret_store"],
        pro["secret_store"],
    ) == (True, True, True)


def test_price_is_omitted(client: TestClient) -> None:
    """#1138 / #1141: the OSS Plan page must not surface a price — pricing lives
    on the payment side. The matrix payload carries no price field."""
    free, _basic, _pro, _promax = client.get(ENDPOINT).json()
    assert "price_monthly" not in free
    assert "price" not in free


def test_promax_row_is_pro_or_better(client: TestClient) -> None:
    """#1548: the XL tier is served last and never below pro on any row."""
    _free, _basic, pro, promax = client.get(ENDPOINT).json()
    assert promax["name"] == "promax"
    assert (promax["max_contexts"], promax["memory_limit"], promax["max_members"]) == (
        1000,
        100000,
        50,
    )
    for key, value in pro.items():
        if isinstance(value, bool):
            assert promax[key] or not value, key
        elif isinstance(value, int):
            assert promax[key] >= value, key
