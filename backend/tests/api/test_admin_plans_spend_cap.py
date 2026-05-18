"""API tests for the admin embedding spend-cap endpoint (Issue #709).

Covers ``PUT /api/v1/admin/plans/workspaces/{workspace_id}/spend-cap``:

- 404 when the workspace is missing.
- 400 when an override exceeds the workspace's current tier default
  (tier-bounded edit affordance; the ``_reject_above_tier`` helper).
- 422 when Pydantic rejects a negative value at parse time (``Field(ge=0)``).
- 200 for the happy paths:
    * Set both daily + monthly overrides to positive values below tier.
    * Clear both overrides (``null``) — inherit tier default.
    * Zero override — admin explicitly disables embedding for the
      workspace (distinct from ``None``-inherit).

Direct ``TestClient`` pattern mirroring ``test_admin_plans_tiers.py`` and
``test_admin_signup_gate.py``. ``get_db`` is overridden with a ``MagicMock``
whose ``execute`` chain returns a controllable workspace stub — the route's
Workspace SELECT, the override write, and ``db.commit()`` are all observed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db

WORKSPACE_ID = str(uuid4())
SPEND_CAP_URL = f"/api/v1/admin/plans/workspaces/{WORKSPACE_ID}/spend-cap"


def _mock_admin_user() -> dict:
    return {"user_id": "admin_user_1", "email": "admin@test.invalid", "role": "admin"}


def _make_workspace_stub(
    *,
    plan_name: str = "pro",
    daily_override: Decimal | None = None,
    monthly_override: Decimal | None = None,
    name: str = "test-ws",
) -> MagicMock:
    """Stub a Workspace ORM row with only the fields the route handler reads."""
    ws = MagicMock()
    ws.id = WORKSPACE_ID
    ws.name = name
    ws.plan_name = plan_name
    ws.embedding_daily_cap_usd = daily_override
    ws.embedding_monthly_cap_usd = monthly_override
    return ws


def _make_db(workspace: Any) -> MagicMock:
    """Build a MagicMock DB session whose execute() returns the workspace stub.

    The route runs exactly one ``select(Workspace).where(...)`` SELECT and
    then writes back via ORM attribute assignment, so a single ``execute``
    return path is enough. ``commit`` and ``rollback`` are AsyncMocks
    because ``db_transaction`` may call them.
    """
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=workspace)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def client_with_db():
    """Yield a TestClient + a closure that installs a workspace stub.

    Tests call ``install(workspace_stub)`` BEFORE making the PUT so the
    ``get_db`` dependency override returns the right mock.
    """
    current_db: dict[str, MagicMock] = {}

    async def mock_admin():
        return _mock_admin_user()

    async def mock_db():
        # Re-yield the most recently installed DB on each request.
        yield current_db["db"]

    app.dependency_overrides[require_admin] = mock_admin
    app.dependency_overrides[get_db] = mock_db

    def install(workspace: Any) -> MagicMock:
        db = _make_db(workspace)
        current_db["db"] = db
        return db

    try:
        yield TestClient(app, raise_server_exceptions=False), install
    finally:
        app.dependency_overrides.clear()


class TestSpendCapEndpointValidation:
    """Pydantic-level rejection before the route handler runs."""

    def test_negative_daily_rejected_with_422(self, client_with_db):
        client, install = client_with_db
        install(_make_workspace_stub())
        resp = client.put(
            SPEND_CAP_URL,
            json={
                "embedding_daily_cap_usd": -0.01,
                "embedding_monthly_cap_usd": None,
            },
        )
        # ``Field(None, ge=0)`` rejects negatives at parse time.
        assert resp.status_code == 422

    def test_negative_monthly_rejected_with_422(self, client_with_db):
        client, install = client_with_db
        install(_make_workspace_stub())
        resp = client.put(
            SPEND_CAP_URL,
            json={
                "embedding_daily_cap_usd": None,
                "embedding_monthly_cap_usd": -1,
            },
        )
        assert resp.status_code == 422


class TestSpendCapEndpointNotFound:
    def test_returns_404_when_workspace_missing(self, client_with_db):
        client, install = client_with_db
        install(workspace=None)  # SELECT returns no row
        resp = client.put(
            SPEND_CAP_URL,
            json={"embedding_daily_cap_usd": 1.0, "embedding_monthly_cap_usd": 30.0},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Workspace not found"


class TestSpendCapEndpointTierBounded:
    """Override values above the tier default are rejected (Issue #709)."""

    def test_above_tier_daily_rejected_with_400(self, client_with_db):
        client, install = client_with_db
        db = install(_make_workspace_stub(plan_name="pro"))
        # Pro tier default = $10/day. Request $20 → must reject.
        resp = client.put(
            SPEND_CAP_URL,
            json={"embedding_daily_cap_usd": 20.0, "embedding_monthly_cap_usd": None},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "embedding_daily_cap_usd" in detail
        assert "tier default" in detail
        # Rejection must happen before any commit.
        db.commit.assert_not_called()

    def test_above_tier_monthly_rejected_with_400(self, client_with_db):
        client, install = client_with_db
        db = install(_make_workspace_stub(plan_name="pro"))
        # Pro tier default = $300/month. Request $5000 → must reject.
        resp = client.put(
            SPEND_CAP_URL,
            json={
                "embedding_daily_cap_usd": None,
                "embedding_monthly_cap_usd": 5000.0,
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "embedding_monthly_cap_usd" in detail
        db.commit.assert_not_called()

    def test_at_tier_boundary_accepted(self, client_with_db):
        """Setting the override equal to the tier default is allowed (boundary)."""
        client, install = client_with_db
        db = install(_make_workspace_stub(plan_name="pro"))
        resp = client.put(
            SPEND_CAP_URL,
            json={"embedding_daily_cap_usd": 10.0, "embedding_monthly_cap_usd": 300.0},
        )
        assert resp.status_code == 200
        db.commit.assert_awaited_once()


class TestSpendCapEndpointHappyPath:
    def test_set_both_overrides_below_tier(self, client_with_db):
        client, install = client_with_db
        workspace = _make_workspace_stub(plan_name="pro")
        db = install(workspace)
        resp = client.put(
            SPEND_CAP_URL,
            json={"embedding_daily_cap_usd": 5.5, "embedding_monthly_cap_usd": 150.0},
        )
        assert resp.status_code == 200
        # ORM assignment must use Decimal (matches NUMERIC column type).
        assert workspace.embedding_daily_cap_usd == Decimal("5.5")
        assert workspace.embedding_monthly_cap_usd == Decimal("150.0")
        db.commit.assert_awaited_once()

    def test_clear_overrides_with_null(self, client_with_db):
        """``null`` removes the override and falls back to tier default."""
        client, install = client_with_db
        workspace = _make_workspace_stub(
            plan_name="pro",
            daily_override=Decimal("5.0"),
            monthly_override=Decimal("150.0"),
        )
        db = install(workspace)
        resp = client.put(
            SPEND_CAP_URL,
            json={
                "embedding_daily_cap_usd": None,
                "embedding_monthly_cap_usd": None,
            },
        )
        assert resp.status_code == 200
        assert workspace.embedding_daily_cap_usd is None
        assert workspace.embedding_monthly_cap_usd is None
        db.commit.assert_awaited_once()

    def test_zero_override_disables_embedding_for_workspace(self, client_with_db):
        """``0`` is distinct from ``null`` — explicitly disables embedding spend."""
        client, install = client_with_db
        workspace = _make_workspace_stub(plan_name="pro")
        db = install(workspace)
        resp = client.put(
            SPEND_CAP_URL,
            json={"embedding_daily_cap_usd": 0, "embedding_monthly_cap_usd": 0},
        )
        assert resp.status_code == 200
        assert workspace.embedding_daily_cap_usd == Decimal("0")
        assert workspace.embedding_monthly_cap_usd == Decimal("0")
        db.commit.assert_awaited_once()

    def test_response_message_includes_workspace_name(self, client_with_db):
        client, install = client_with_db
        workspace = _make_workspace_stub(plan_name="pro", name="acme-prod")
        install(workspace)
        resp = client.put(
            SPEND_CAP_URL,
            json={"embedding_daily_cap_usd": 1.0, "embedding_monthly_cap_usd": 30.0},
        )
        assert resp.status_code == 200
        assert "acme-prod" in resp.json()["message"]


class TestSpendCapEndpointWithFreeTier:
    """Free tier default = $0.50/day; any override above 0.50 must reject."""

    def test_free_tier_caps_lower_override_only(self, client_with_db):
        client, install = client_with_db
        db = install(_make_workspace_stub(plan_name="free"))
        # Free tier daily default is $0.50; request $1.00 → above tier → reject.
        resp = client.put(
            SPEND_CAP_URL,
            json={
                "embedding_daily_cap_usd": 1.0,
                "embedding_monthly_cap_usd": None,
            },
        )
        assert resp.status_code == 400
        db.commit.assert_not_called()

    def test_free_tier_allows_clearing_override(self, client_with_db):
        client, install = client_with_db
        workspace = _make_workspace_stub(plan_name="free", daily_override=Decimal("0.25"))
        db = install(workspace)
        resp = client.put(
            SPEND_CAP_URL,
            json={
                "embedding_daily_cap_usd": None,
                "embedding_monthly_cap_usd": None,
            },
        )
        assert resp.status_code == 200
        assert workspace.embedding_daily_cap_usd is None
        db.commit.assert_awaited_once()
