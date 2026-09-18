"""Tests for the ENABLE_COST_DISPLAY deployment flag (#1571).

``ENABLE_COST_DISPLAY=false`` hides money from non-system-admin surfaces on
a flat-price hosted deployment (the platform-billed USD is the operator's
own cost, not something to show a customer):

- ``GET /workspaces/{id}/cost-aggregation`` returns 404 (same
  feature-not-present semantics as ``ENABLE_BYOK`` #1167, gate runs before
  auth so every caller sees the same 404),
- ``GET /admin/cost-aggregation`` stays available (operators need it),
- analysis REST payloads keep their shape but carry ``null`` cost fields
  (``AnalysisRow.cost_*_cents``, ``AnalysisPreviewResponse.estimated_cost_cents``),
- ``/system/info`` exposes ``features.cost_display`` (default ON — OSS
  unchanged) so the web UI can hide the cost page / KPI / column / estimate.

The MCP mirror (keys OMITTED rather than nulled) is pinned in
``tests/mcp_server/test_cost_display_gate.py``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.analysis_gates import require_memory_analysis_access, require_memory_analysis_read
from db.base import get_db

_WORKSPACE_ID = uuid4()
_CONTEXT_ID = uuid4()
_USER_ID = "test_user_cost_display"


@pytest.fixture
def client():
    """TestClient that auto-clears dependency overrides on teardown."""
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def cost_display_disabled(monkeypatch):
    """Rebuild the settings singleton with ENABLE_COST_DISPLAY=false.

    Same shape as ``byok_disabled`` in ``test_byok_feature_gate.py``:
    ``monkeypatch`` restores the env var and the singleton on teardown.
    """
    monkeypatch.setenv("ENABLE_COST_DISPLAY", "false")
    monkeypatch.setattr("config.settings._settings", None)


class TestSystemInfoCostDisplayFlag:
    def test_features_cost_display_defaults_on(self, client):
        features = client.get("/api/v1/system/info").json()["features"]
        assert features.get("cost_display") is True, "ENABLE_COST_DISPLAY must default ON (OSS)"

    def test_features_cost_display_reflects_disabled(self, client, cost_display_disabled):
        features = client.get("/api/v1/system/info").json()["features"]
        assert features.get("cost_display") is False


class TestCostAggregationGate:
    def test_workspace_route_returns_404_when_disabled(self, client, cost_display_disabled):
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 404

    def test_workspace_route_exists_when_enabled(self, client):
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 401

    def test_admin_route_unaffected_when_disabled(self, client, cost_display_disabled):
        # /admin/cost-aggregation is intentionally NOT gated: anonymous gets
        # the normal 401 (route present), never the feature-gate 404.
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Analysis REST payloads: shape stable, cost fields null
# ---------------------------------------------------------------------------


def _scalar_one(value):
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)
    res.scalar = MagicMock(return_value=value)
    return res


def _scalars_all(values: list) -> MagicMock:
    res = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=values)
    res.scalars = MagicMock(return_value=scalars)
    return res


def _pricing_row(unit_type: str, price: str) -> MagicMock:
    """A gpt-5-nano ``LLMPricing`` row as ``try_resolve_pricing_row`` reads it
    (same shape as ``test_analyses_routes.py``)."""
    row = MagicMock()
    row.provider = "openai"
    row.model = "gpt-5-nano"
    row.unit_type = unit_type
    row.price_per_unit = Decimal(price)
    row.unit_denominator = 1_000_000
    row.effective_from = datetime(2026, 4, 28)
    return row


_PRICED_ROWS = [_pricing_row("input_tokens", "0.2"), _pricing_row("output_tokens", "1.25")]


def _fake_run() -> MagicMock:
    return MagicMock(
        id=uuid4(),
        workspace_id=_WORKSPACE_ID,
        context_id=_CONTEXT_ID,
        status="succeeded",
        triggered_by=_USER_ID,
        started_at=datetime(2026, 5, 2),
        finished_at=datetime(2026, 5, 2),
        input_count=10,
        cost_estimated_cents=5,
        cost_actual_cents=4,
        error=None,
        cancellation_reason=None,
    )


@pytest.fixture
def analyses_client(client):
    """``client`` with the analysis gates + DB bypassed (pattern from
    ``test_analyses_routes.py``); the context-boundary lookup passes."""
    db_mock = MagicMock()
    db_mock.execute = AsyncMock()
    db_mock.commit = AsyncMock()

    async def _gate():
        return (_USER_ID, _WORKSPACE_ID, "UTC")

    async def _db():
        yield db_mock

    app.dependency_overrides[require_memory_analysis_access] = _gate
    app.dependency_overrides[require_memory_analysis_read] = _gate
    app.dependency_overrides[get_db] = _db
    return client, db_mock


class TestAnalysisRestPayloadsWhenDisabled:
    def test_list_rows_carry_null_costs_but_keep_keys(self, analyses_client, cost_display_disabled):
        client, db_mock = analyses_client
        db_mock.execute.side_effect = [_scalar_one(_CONTEXT_ID)]
        with patch(
            "services.analysis.query_service.list_analyses",
            AsyncMock(return_value=([_fake_run()], None)),
        ):
            response = client.get(f"/api/v1/contexts/{_CONTEXT_ID}/analyses")
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        # Shape stable: the keys are present …
        assert "cost_estimated_cents" in item
        assert "cost_actual_cents" in item
        # … but carry null; the non-money aggregate is untouched.
        assert item["cost_estimated_cents"] is None
        assert item["cost_actual_cents"] is None
        assert item["input_count"] == 10

    def test_single_run_carries_null_costs(self, analyses_client, cost_display_disabled):
        client, db_mock = analyses_client
        run = _fake_run()
        db_mock.execute.side_effect = [_scalar_one(_CONTEXT_ID)]
        with patch(
            "services.analysis.query_service.get_analysis",
            AsyncMock(return_value=run),
        ):
            response = client.get(f"/api/v1/contexts/{_CONTEXT_ID}/analyses/{run.id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["cost_estimated_cents"] is None
        assert body["cost_actual_cents"] is None

    def test_active_run_carries_null_costs(self, analyses_client, cost_display_disabled):
        client, db_mock = analyses_client
        db_mock.execute.side_effect = [_scalar_one(_CONTEXT_ID)]
        with patch(
            "services.analysis.query_service.get_active_analysis",
            AsyncMock(return_value=_fake_run()),
        ):
            response = client.get(f"/api/v1/contexts/{_CONTEXT_ID}/analyses/active")
        assert response.status_code == 200, response.text
        assert response.json()["cost_actual_cents"] is None

    def test_preview_estimate_is_null(self, analyses_client, cost_display_disabled):
        client, db_mock = analyses_client
        db_mock.execute.side_effect = [
            _scalar_one(_CONTEXT_ID),  # boundary
            _scalar_one(100),  # count_context_memories
            _scalar_one(1),  # #1569 lane: an enabled OpenAI key exists → BYOK
            _scalars_all(_PRICED_ROWS),  # a priced model — would quote >= 1
        ]
        response = client.post(f"/api/v1/contexts/{_CONTEXT_ID}/analyses/preview", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert "estimated_cost_cents" in body
        assert body["estimated_cost_cents"] is None
        # The non-money preview fields are still served.
        assert body["memory_count"] == 100
        assert body["model_id"] == "gpt-5-nano"


class TestAnalysisRestPayloadsWhenEnabled:
    def test_preview_quotes_a_price_by_default(self, analyses_client):
        """Control for the null-estimate test above: the same pricing rows
        DO produce a positive estimate when the flag is on."""
        client, db_mock = analyses_client
        db_mock.execute.side_effect = [
            _scalar_one(_CONTEXT_ID),
            _scalar_one(100),
            _scalar_one(1),  # #1569 lane: BYOK key exists
            _scalars_all(_PRICED_ROWS),
        ]
        response = client.post(f"/api/v1/contexts/{_CONTEXT_ID}/analyses/preview", json={})
        assert response.status_code == 200, response.text
        assert response.json()["estimated_cost_cents"] >= 1

    def test_list_rows_keep_costs_by_default(self, analyses_client):
        client, db_mock = analyses_client
        db_mock.execute.side_effect = [_scalar_one(_CONTEXT_ID)]
        with patch(
            "services.analysis.query_service.list_analyses",
            AsyncMock(return_value=([_fake_run()], None)),
        ):
            response = client.get(f"/api/v1/contexts/{_CONTEXT_ID}/analyses")
        item = response.json()["items"][0]
        assert item["cost_estimated_cents"] == 5
        assert item["cost_actual_cents"] == 4
