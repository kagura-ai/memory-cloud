"""A ``PLAN_<KEY>_FEATURES`` override reaches the "may create" gates (#1559).

Mirrors ``test_xl_only_create_gates.TestResourceTokenCreate``: the route
consults ``has_feature`` at call time, so a self-host that re-enables
``resources`` on M via the env override mints a resource token on a basic
workspace — the #1551 XL-only refusal is the code default, not a hardcode.
Mock-based (no DB).
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# A feature dropped from EVERY tier still refuses cleanly at the MCP gates
# ---------------------------------------------------------------------------


@pytest.fixture
def public_features_on_no_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ``public_contexts`` / ``resources`` / ``connectors`` from XL — they
    are then on no tier at all and have no ``FEATURE_MIN_PLANS`` row."""
    monkeypatch.setattr(plan_tiers, "PLAN_TIERS", dict(plan_tiers.PLAN_TIERS))
    monkeypatch.setattr(plan_tiers, "FEATURE_MIN_PLANS", dict(plan_tiers.FEATURE_MIN_PLANS))
    monkeypatch.setattr(plan_tiers, "logger", MagicMock())
    plan_tiers._apply_settings_overrides(
        Settings(
            _env_file=None,
            plan_promax_features=(
                "api_keys,oauth,reranking,managed_embeddings,secret_store,"
                "team_invitations,shared_contexts,memory_analysis"
            ),
        )
    )
    assert "public_contexts" not in plan_tiers.FEATURE_MIN_PLANS


@pytest.mark.asyncio
async def test_public_flag_gate_refuses_a_feature_on_no_tier_without_raising(
    public_features_on_no_tier: None,
) -> None:
    """The MCP gate must still answer with the ``plan_required`` envelope —
    ``required_plan`` is ``null`` and the text takes the "higher" fallback —
    instead of raising ``ValueError: Unknown feature`` into the handler's
    catch-all (an ``update_context_error`` with a stack trace)."""
    from mcp_server.tools.context import _apply_public_flag

    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(plan_name="promax"))
    ctx = SimpleNamespace(workspace_id="ws-1", is_public=False, resource_id=None)

    error = await _apply_public_flag(db, ctx, True)

    assert error is not None
    payload = json.loads(error[0].text)
    assert payload["error"] == "plan_required"
    assert payload["required_plan"] is None
    assert "higher plan" in payload["message"]
    assert ctx.is_public is False


@pytest.mark.asyncio
async def test_setup_connector_gate_refuses_a_feature_on_no_tier_without_raising(
    public_features_on_no_tier: None,
) -> None:
    """``setup_connector`` builds its envelope INSIDE the exception handler, so
    an unguarded lookup there escaped the tool entirely. Same mocks as
    ``test_setup_connector_plan_gate``: the service refuses right after the
    workspace lookup, nothing staged or committed."""
    from mcp_server.tools.resource import handle_setup_connector

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[_result(one=SimpleNamespace(plan_name="promax", effective_max_connectors=50))]
    )
    db.add = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()

    async def _gen():
        yield db

    with (
        patch("db.base.get_db", side_effect=lambda: _gen()),
        patch(
            "mcp_server.tools.resource._check_owner_admin_role", new=AsyncMock(return_value=None)
        ),
        patch("mcp_server.tools.resource._log_tool_usage", new=AsyncMock()),
        patch("services.worker_app_identity.WorkerAppIdentityService") as identity,
    ):
        identity.return_value.get_identity = AsyncMock(return_value=None)
        result = await handle_setup_connector(
            {"connector_type": "slack", "resource_id": "slack_general"}, "user-1", _WS
        )

    payload = json.loads(result[0].text)
    assert payload["error"] == "plan_required"
    assert payload["required_plan"] is None
    assert payload["feature"] == "connectors"
    assert "higher plan" in payload["message"]
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# REST shared-context gates name the EFFECTIVE minimum tier
# ---------------------------------------------------------------------------


@pytest.fixture
def basic_may_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PLAN_BASIC_FEATURES`` adds ``shared_contexts`` to M."""
    monkeypatch.setattr(plan_tiers, "PLAN_TIERS", dict(plan_tiers.PLAN_TIERS))
    monkeypatch.setattr(plan_tiers, "FEATURE_MIN_PLANS", dict(plan_tiers.FEATURE_MIN_PLANS))
    monkeypatch.setattr(plan_tiers, "logger", MagicMock())
    plan_tiers._apply_settings_overrides(
        Settings(
            _env_file=None,
            plan_basic_features=(
                "api_keys,oauth,reranking,managed_embeddings,secret_store,shared_contexts"
            ),
        )
    )


def _existing_context() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="ctx",
        display_name=None,
        description=None,
        summary=None,
        usage_guide=None,
        is_default=False,
        is_locked=False,
        sleep_mode="skip",
        is_private=True,
        is_public=False,
        resource_id=None,
        created_by="owner-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        workspace_id=_WS,
    )


async def _put_shared(plan_name: str) -> MagicMock:
    """``PUT /contexts/{id}`` with ``is_private=False`` — the SHARED gate."""
    from api.routes.contexts import ContextUpdate, update_context

    existing = _existing_context()
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(plan_name=plan_name))
    perm = MagicMock()
    perm.check_context_owner = AsyncMock(return_value=existing)
    service = MagicMock()
    service.update_context = AsyncMock(return_value=existing)

    with patch("services.permission_service.PermissionService", return_value=perm):
        await update_context(
            existing.id,
            ContextUpdate(is_private=False),
            {"user_id": "owner-1", "sub": "owner-1"},
            service,
            db,
        )
    return service


async def _post_shared(plan_name: str) -> None:
    """``POST /contexts`` with ``is_private=False`` — the SHARED gate."""
    from api.routes.contexts import ContextCreate, create_context

    service = MagicMock()
    service.db.execute = AsyncMock(return_value=_result(one=SimpleNamespace(plan_name=plan_name)))
    with patch(
        "services.quota_service.QuotaService.check_context_creation_allowed",
        new=AsyncMock(return_value=(True, None)),
    ):
        await create_context(
            ContextCreate(name="ctx", is_private=False),
            {"user_id": "owner-1", "sub": "owner-1", "current_workspace_id": _WS},
            service,
        )


@pytest.mark.asyncio
async def test_shared_refusals_name_the_registry_tier_by_default() -> None:
    """Without an override both REST gates name L from the registry — the
    hard-coded "Pro plan" text (a tier name that no longer exists) is gone."""
    from fastapi import HTTPException

    upgrade_to_l = f"Upgrade to {plan_tiers.get_plan_tier('pro').display_name} plan"

    with pytest.raises(HTTPException) as post_exc:
        await _post_shared("free")
    assert post_exc.value.status_code == 403
    assert upgrade_to_l in post_exc.value.detail
    assert "Pro plan" not in post_exc.value.detail

    with pytest.raises(HTTPException) as put_exc:
        await _put_shared("free")
    assert put_exc.value.status_code == 400
    assert upgrade_to_l in put_exc.value.detail
    assert "Pro plan" not in put_exc.value.detail


@pytest.mark.asyncio
async def test_shared_refusal_follows_the_override_and_basic_passes(
    basic_may_share: None,
) -> None:
    """With ``shared_contexts`` on M, Free is told to upgrade to M (not L) and
    M itself passes — ``allows_shared_contexts`` moved with the feature."""
    from fastapi import HTTPException

    upgrade_to_m = f"Upgrade to {plan_tiers.get_plan_tier('basic').display_name} plan"

    with pytest.raises(HTTPException) as exc_info:
        await _put_shared("free")
    assert upgrade_to_m in exc_info.value.detail

    service = await _put_shared("basic")
    service.update_context.assert_awaited_once()
