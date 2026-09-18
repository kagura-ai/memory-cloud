"""API tests for ``GET /workspaces/{workspace_id}/plan`` — the ``quotas`` contract (#1560).

The frontend token screens read two keys off this payload by name:
``quotas.max_resource_tokens`` (the "used / max" figure) and
``quotas.max_quota_capacity`` (the quota-capacity line and the create dialog's
bound). A backend rename would not fail on its own — the UI silently falls back
to its "cap unknown" state — so the keys are pinned here.

Direct TestClient pattern (mirrors ``test_workspace_plan_tiers``): the owner
check is patched out and the DB is a stub whose ``execute`` yields the
workspace row, the member ids and the context count in the order the route
issues them, so no live database is needed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_session_auth
from config.plan_tiers import get_plan_tier
from db.base import get_db

WORKSPACE_ID = uuid4()
ENDPOINT = f"/api/v1/workspaces/{WORKSPACE_ID}/plan"

# Mirrors ``WorkspacePlanInfo.quotas`` in ``frontend/src/lib/api/workspaces.ts``.
FRONTEND_QUOTA_KEYS = {
    "memory_limit",
    "max_contexts",
    "max_resource_tokens",
    "max_quota_capacity",
    "mcp_calls_per_day",
    "mcp_calls_per_week",
    "rest_calls_per_day",
    "public_calls_per_day",
}


def _workspace(plan_name: str) -> SimpleNamespace:
    """Workspace row stub whose ``effective_*`` properties are the tier defaults."""
    tier = get_plan_tier(plan_name)
    return SimpleNamespace(
        id=WORKSPACE_ID,
        name="ws",
        plan_name=plan_name,
        effective_memory_limit=tier.memory_limit,
        effective_max_contexts=tier.max_contexts_per_workspace,
        effective_mcp_calls_per_day=tier.mcp_calls_per_day,
        effective_mcp_calls_per_week=tier.mcp_calls_per_week,
        effective_rest_calls_per_day=tier.rest_calls_per_day,
        effective_public_calls_per_day=tier.public_calls_per_day,
    )


def _result(**attrs: object) -> MagicMock:
    """A ``db.execute`` result whose named accessors return the given values."""
    result = MagicMock()
    for name, value in attrs.items():
        getattr(result, name).return_value = value
    return result


@pytest.fixture
def make_client() -> Iterator[Callable[[str], TestClient]]:
    async def mock_session():
        return {"user_id": "owner-1", "email": "owner@test.invalid"}

    perm_patcher = patch("api.routes.workspace_plan.PermissionService")
    perm_cls = perm_patcher.start()
    perm_cls.return_value.check_workspace_owner = AsyncMock(return_value=None)

    def _make(plan_name: str) -> TestClient:
        db = MagicMock()
        # Route order: workspace row → member ids (empty, so the memory count
        # query is skipped) → context count.
        db.execute = AsyncMock(
            side_effect=[
                _result(scalar_one_or_none=_workspace(plan_name)),
                _result(all=[]),
                _result(scalar=0),
            ]
        )

        async def mock_db():
            yield db

        app.dependency_overrides[require_session_auth] = mock_session
        app.dependency_overrides[get_db] = mock_db
        return TestClient(app, raise_server_exceptions=False)

    yield _make
    perm_patcher.stop()
    app.dependency_overrides.clear()


def test_quotas_carry_the_resource_token_serve_caps(make_client) -> None:
    resp = make_client("pro").get(ENDPOINT)
    assert resp.status_code == 200, resp.text
    quotas = resp.json()["quotas"]

    tier = get_plan_tier("pro")
    # #1560: the frontend reads these two by name.
    assert quotas["max_resource_tokens"] == tier.max_resource_tokens
    assert quotas["max_quota_capacity"] == tier.max_resource_tokens * 10000
    # SERVE caps stay positive on L for tokens that already exist there, even
    # though the CREATION matrix (/workspaces/plans/tiers) reads 0 for L
    # (#1551) — which is why the token screens cannot use the matrix here.
    assert quotas["max_resource_tokens"] > 0


def test_quotas_shape_matches_the_frontend_type(make_client) -> None:
    resp = make_client("promax").get(ENDPOINT)
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["quotas"]) == FRONTEND_QUOTA_KEYS
