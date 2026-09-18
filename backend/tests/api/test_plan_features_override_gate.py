"""A ``PLAN_<KEY>_FEATURES`` override reaches the "may create" gates (#1559).

Mirrors ``test_xl_only_create_gates.TestResourceTokenCreate``: the route
consults ``has_feature`` at call time, so a self-host that re-enables
``resources`` on M via the env override mints a resource token on a basic
workspace — the #1551 XL-only refusal is the code default, not a hardcode.
Mock-based (no DB).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config.plan_tiers as plan_tiers
from config.settings import Settings
from utils.exceptions import FeatureNotAvailableError

_WS = uuid.uuid4()


def _result(*, one=None, scalar=None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = one
    result.scalar.return_value = scalar
    return result


def _token() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        resource_id="products",
        resource_pk=uuid.uuid4(),
        description=None,
        quota_events_per_hour=1000,
        created_by="owner-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_used_at=None,
        is_active=True,
    )


async def _create(plan_name: str):
    from api.routes.resource_tokens import ResourceTokenCreate, create_resource_token

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    # 1) context exists in workspace → id, 2) plan_name, 3) active count
    db.execute = AsyncMock(
        side_effect=[_result(one=uuid.uuid4()), _result(one=plan_name), _result(scalar=0)]
    )
    manager = MagicMock()
    manager.create_token = AsyncMock(return_value=("kagura_resource_plain", _token()))

    with patch(
        "api.routes.resource_tokens.resolve_resource_pk",
        new=AsyncMock(return_value=uuid.uuid4()),
    ):
        response = await create_resource_token(
            ResourceTokenCreate(resource_id="products", quota_events_per_hour=1000),
            ("owner-1", _WS),
            manager,
            db,
        )
    return response, manager


@pytest.fixture
def basic_may_create_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply ``PLAN_BASIC_FEATURES`` to isolated copies of the registry."""
    monkeypatch.setattr(plan_tiers, "PLAN_TIERS", dict(plan_tiers.PLAN_TIERS))
    monkeypatch.setattr(plan_tiers, "FEATURE_MIN_PLANS", dict(plan_tiers.FEATURE_MIN_PLANS))
    monkeypatch.setattr(plan_tiers, "logger", MagicMock())
    plan_tiers._apply_settings_overrides(
        Settings(
            _env_file=None,
            plan_basic_features=(
                "api_keys,oauth,reranking,managed_embeddings,secret_store,"
                "resources,connectors,public_contexts"
            ),
        )
    )


@pytest.mark.asyncio
async def test_basic_is_refused_without_the_override() -> None:
    with pytest.raises(FeatureNotAvailableError) as exc_info:
        await _create("basic")
    assert exc_info.value.details["feature"] == "resources"


@pytest.mark.asyncio
async def test_basic_with_the_override_mints_a_resource_token(
    basic_may_create_resources: None,
) -> None:
    response, manager = await _create("basic")
    assert response.token == "kagura_resource_plain"
    manager.create_token.assert_awaited_once()
    # The tier's own numeric cap (M: 3 active tokens) is still the second gate.
    assert plan_tiers.get_plan_tier("basic").max_resource_tokens == 3


@pytest.mark.asyncio
async def test_free_stays_refused_and_names_the_new_minimum_tier(
    basic_may_create_resources: None,
) -> None:
    """The refusal on a tier below the override names the EFFECTIVE minimum
    (M), not the code-default XL."""
    with pytest.raises(FeatureNotAvailableError) as exc_info:
        await _create("free")
    assert plan_tiers.get_plan_tier("basic").display_name in exc_info.value.message
    assert "XL" not in exc_info.value.message
