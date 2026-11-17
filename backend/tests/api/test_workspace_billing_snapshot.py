"""Strict new contract and unchanged billing-only authentication boundary."""

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from api.routes import internal_integrations
from config.billing_addons import ADDON_COLUMNS, ADDON_MAX_BY_KEY
from config.settings import get_settings
from db.base import get_db
from models.workspace_billing_schemas import BILLING_V1_ADDON_KEYS, WorkspaceBillingPush
from utils.exceptions import MemoryCloudException

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/workspace-billing-snapshot-v1.json"
PATH = "/internal/workspaces/11111111-1111-4111-8111-111111111111/billing-snapshot"


def body():
    return json.loads(FIXTURE.read_text())["snapshot"]


def test_shared_wire_is_a_strict_complete_normalized_snapshot():
    result = WorkspaceBillingPush.model_validate(body())
    assert result.contract_version == "workspace-billing-snapshot-v1"
    assert result.plan.addons["history_retention_days"] == 0
    assert result.max_access.valid_from.utcoffset().total_seconds() == 0
    assert result.model_dump(mode="json") == body()


def test_plan_is_required_but_nullable_with_distinct_digest():
    from services.integration_entitlements import _digest

    full = WorkspaceBillingPush.model_validate(body())
    value = {**body(), "plan": None}
    rights = WorkspaceBillingPush.model_validate(value)
    assert rights.plan is None and rights.model_dump(mode="json")["plan"] is None
    wid = UUID("11111111-1111-4111-8111-111111111111")
    assert _digest(wid, full) != _digest(wid, rights)
    value.pop("plan")
    with pytest.raises(ValidationError):
        WorkspaceBillingPush.model_validate(value)


def test_v1_addon_keys_are_frozen_while_storage_and_domain_maps_can_grow(monkeypatch):
    monkeypatch.setitem(ADDON_COLUMNS, "future", "addon_future_bonus")
    monkeypatch.setitem(ADDON_MAX_BY_KEY, "future", 2)
    value = body()
    assert set(WorkspaceBillingPush.model_validate(value).plan.addons) == set(BILLING_V1_ADDON_KEYS)
    value["plan"]["addons"]["future"] = 0
    with pytest.raises(ValidationError):
        WorkspaceBillingPush.model_validate(value)


def test_nonnull_fixture_digest_preserves_pre_review_applied_receipts():
    from services.integration_entitlements import _digest

    # Computed with the actual _digest from reviewed commit37c91b35; nullable
    # plan support must not turn retries of its non-null deliveries into409s.
    assert (
        _digest(
            UUID("11111111-1111-4111-8111-111111111111"),
            WorkspaceBillingPush.model_validate(body()),
        )
        == "025120e7bc84c5954b648df2fcbe0388b51bfc45ad0b672bc6046e171aec486d"
    )


@pytest.mark.parametrize(
    "changed",
    [
        "missing_addon",
        "extra_addon",
        "bool",
        "float",
        "negative",
        "huge",
        "history",
        "plan",
        "extra",
        "plan_extra",
        "version",
        "partial",
        "duplicate_service",
        "naive",
        "empty_window",
        "too_many_services",
    ],
)
def test_incomplete_or_ambiguous_wire_is_rejected(changed):
    value = body()
    if changed == "missing_addon":
        value["plan"]["addons"].pop("history_retention_days")
    elif changed == "extra_addon":
        value["plan"]["addons"]["arbitrary"] = 1
    elif changed in ("bool", "float", "negative", "huge"):
        value["plan"]["addons"]["memory"] = {
            "bool": True,
            "float": 1.0,
            "negative": -1,
            "huge": 2_000_000_001,
        }[changed]
    elif changed == "history":
        value["plan"]["addons"]["history_retention_days"] = 3651
    elif changed == "plan":
        value["plan"]["plan_name"] = "enterprise"
    elif changed == "extra":
        value["owner"] = True
    elif changed == "plan_extra":
        value["plan"]["stripe_customer_id"] = "cus_forbidden"
    elif changed == "version":
        value["contract_version"] = "v2"
    elif changed == "partial":
        value.pop("services")
    elif changed == "duplicate_service":
        value["services"].append(copy.deepcopy(value["services"][0]))
    elif changed == "naive":
        value["max_access"]["valid_from"] = "2026-11-14T00:00:00"
    elif changed == "empty_window":
        value["max_access"]["valid_until"] = value["max_access"]["valid_from"]
    else:
        value["services"] = [
            {**value["services"][0], "service_id": f"service-{number}"} for number in range(33)
        ]
    with pytest.raises(ValidationError):
        WorkspaceBillingPush.model_validate(value)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(get_settings(), "billing_service_token", "synthetic-billing")
    app = FastAPI()
    app.include_router(internal_integrations.router)
    opened = []

    @app.exception_handler(MemoryCloudException)
    async def canonical(_request, exc):
        return JSONResponse({"error": exc.error_code}, status_code=exc.status_code)

    async def sessions():
        opened.append(True)
        yield AsyncMock()

    app.dependency_overrides[get_db] = sessions
    return app, opened


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize(
    "credential", [None, "Basic bad", "Bearer oidc_access", "Bearer browser", "Bearer worker"]
)
async def test_new_billing_route_rejects_non_publisher_authority_before_db(api, method, credential):
    headers = {"Cookie": "kagura_session=synthetic"}
    if credential:
        headers["Authorization"] = credential
    async with AsyncClient(
        transport=ASGITransport(app=api[0]), base_url="https://testserver"
    ) as web:
        response = await web.request(
            method, PATH, json=body() if method == "PUT" else None, headers=headers
        )
    assert response.status_code == 401 and not api[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_new_billing_route_missing_config_and_duplicate_credentials_fail_closed(
    api, monkeypatch, method
):
    async with AsyncClient(
        transport=ASGITransport(app=api[0]), base_url="https://testserver"
    ) as web:
        response = await web.request(
            method,
            PATH,
            json=body() if method == "PUT" else None,
            headers=[("Authorization", "Bearer synthetic-billing")] * 2,
        )
        assert response.status_code == 401 and not api[1]
        monkeypatch.setattr(get_settings(), "billing_service_token", "")
        response = await web.request(
            method,
            PATH,
            json=body() if method == "PUT" else None,
            headers={"Authorization": "Bearer synthetic-billing"},
        )
    assert response.status_code == 503 and not api[1]
