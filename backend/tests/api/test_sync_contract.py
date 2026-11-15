"""Producer metadata and independent consumer-profile negotiation (#2811)."""

import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import well_known
from config.settings import Settings
from models.sync_feed_schemas import FeedRecord
from services.sync_contract import (
    REQUIRED_SYNC_CAPABILITIES,
    SyncContractUnsupported,
    require_sync_contract,
    sync_configuration,
)

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/sync-contract-v1.json").read_text())
FLAGS = (
    "oidc_enabled",
    "sync_integrations_enabled",
    "sync_credentials_enabled",
    "sync_runs_enabled",
    "sync_managed_enabled",
    "sync_observability_enabled",
    "mcp_public_endpoints_enabled",
    "mcp_public_usage_budget_enabled",
    "mcp_public_domains_enabled",
)


def settings(**changes):
    """No operator environment or signing key is needed for non-authorizing metadata."""
    return Settings.model_construct(**(dict.fromkeys(FLAGS, True) | changes))


def test_producer_matches_consumer_fixture_without_secrets_or_host_authority(monkeypatch):
    configured = settings()
    configured.oidc_signing_keys_file = "private-key-path-must-not-escape"
    monkeypatch.setattr(well_known, "get_settings", lambda: configured)
    app = FastAPI()
    app.include_router(well_known.router, prefix="/.well-known")
    response = TestClient(app).get(
        "/.well-known/kmc-sync-configuration", headers={"Host": "attacker.invalid"}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json() == FIXTURE["configuration"]
    assert "private-key" not in response.text and "attacker.invalid" not in response.text
    assert require_sync_contract(response.json(), custom_domain=True)


def test_disabled_deployment_cannot_be_mistaken_for_enabled_support():
    disabled = Settings.model_construct(**dict.fromkeys(FLAGS, False))
    payload = sync_configuration(disabled).model_dump()
    assert payload["capabilities"] == []
    with pytest.raises(SyncContractUnsupported, match="unsupported_sync_contract"):
        require_sync_contract(payload)


def test_real_application_publishes_anonymous_discovery_and_openapi_without_new_login(monkeypatch):
    from api.main import app

    for flag in FLAGS:
        monkeypatch.delenv(flag.upper(), raising=False)
    configured = Settings(_env_file=None)
    monkeypatch.setattr(well_known, "get_settings", lambda: configured)
    response = TestClient(app).get("/.well-known/kmc-sync-configuration")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["capabilities"] == []
    contract = app.openapi()["paths"]["/.well-known/kmc-sync-configuration"]["get"]
    assert contract["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SyncConfigurationV1"
    }


@pytest.mark.parametrize("capability", sorted(REQUIRED_SYNC_CAPABILITIES))
def test_consumer_refuses_each_missing_core_capability(capability):
    payload = copy.deepcopy(FIXTURE["configuration"])
    payload["capabilities"].remove(capability)
    with pytest.raises(SyncContractUnsupported):
        require_sync_contract(payload)


def test_standard_url_does_not_require_optional_domain_service():
    payload = copy.deepcopy(FIXTURE["configuration"])
    payload["capabilities"].remove("public.domains.v1")
    assert require_sync_contract(payload)
    with pytest.raises(SyncContractUnsupported):
        require_sync_contract(payload, custom_domain=True)


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": "kmc.sync.v2"},
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_hash": "0" * 64},
        {"human_scopes": ["openid", "workspaces:read"]},
        {"service_scope": "mcp"},
        {"assertion_audience": "token_endpoint"},
        {"capabilities": "all"},
        {"capabilities": None},
    ],
)
def test_consumer_rejects_changed_schema_auth_profile_or_ambiguous_metadata(change):
    payload = {**copy.deepcopy(FIXTURE["configuration"]), **change}
    with pytest.raises(SyncContractUnsupported, match="^unsupported_sync_contract$"):
        require_sync_contract(payload)


@pytest.mark.parametrize("payload", [None, [], {"version": "99.0.0"}, {"protocol": "kmc.sync.v1"}])
def test_version_strings_and_partial_metadata_never_authorize_start(payload):
    with pytest.raises(SyncContractUnsupported):
        require_sync_contract(payload)


def test_schema_fixture_uses_actual_approved_product_and_faq_shapes():
    for kind, record in FIXTURE["records"].items():
        assert FeedRecord.model_validate_json(json.dumps(record)).public_payload(kind)


@pytest.mark.parametrize(
    ("flag", "removed"),
    [
        (
            "oidc_enabled",
            {
                "identity.oidc.v1",
                "workspace.access.v1",
                "sync.credentials.v1",
                "sync.feeds.v1",
                "sync.managed.v1",
                "sync.activity.v1",
            },
        ),
        (
            "sync_integrations_enabled",
            {
                "sync.installations.v1",
                "sync.credentials.v1",
                "sync.feeds.v1",
                "sync.managed.v1",
                "sync.activity.v1",
            },
        ),
        ("sync_credentials_enabled", {"sync.credentials.v1", "sync.feeds.v1", "sync.managed.v1"}),
        ("sync_runs_enabled", {"sync.feeds.v1", "sync.managed.v1"}),
        (
            "mcp_public_endpoints_enabled",
            {
                "public.mcp.v1",
                "public.budget.v1",
                "public.domains.v1",
                "sync.installations.v1",
                "sync.credentials.v1",
                "sync.feeds.v1",
                "sync.managed.v1",
            },
        ),
        ("mcp_public_usage_budget_enabled", {"public.budget.v1", "public.domains.v1"}),
    ],
)
def test_prerequisite_disable_cannot_advertise_dependent_capability(flag, removed):
    configured = settings()
    setattr(configured, flag, False)
    advertised = set(sync_configuration(configured).capabilities)
    assert not removed & advertised
    with pytest.raises(SyncContractUnsupported):
        require_sync_contract(sync_configuration(configured).model_dump())
