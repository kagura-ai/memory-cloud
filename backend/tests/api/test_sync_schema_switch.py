"""``SYNC_SCHEMA_V2_ENABLED``: discovery, the advertised pair and the upgrade route (#3055).

Off (the default) the discovery document is byte-for-byte the previous one for
every switch combination and the upgrade route answers 404 before
authentication. On, ``sync.schema.v2`` is appended last and the pair is
version 2's, exactly while ``sync.feeds.v1`` is advertised.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.routes import sync_records
from config.settings import Settings
from models.sync_contract import SyncConfigurationV1
from models.sync_feed_schemas import SCHEMA_V1, SCHEMA_V2, FeedRecord, SchemaUpgrade
from services.sync_contract import (
    REQUIRED_SYNC_CAPABILITIES,
    SCHEMA_V2_CAPABILITY,
    SyncContractUnsupported,
    new_installation_schema,
    require_sync_contract,
    sync_configuration,
)
from tests.api.test_sync_wire_golden import FLAGS, V0186_ALL_FLAGS, compact, discovery

FIXTURES = Path(__file__).parents[1] / "fixtures"
CONTRACT_V2 = json.loads((FIXTURES / "sync-contract-v2.json").read_text())
V0186_ALL_FLAGS_SWITCH_ON_SHA256 = (
    "59a641c32e6ab2466f4ea0f1e1621080cc73a456a4f0a5ecfb6bb8569555d2f1"
)


def settings(**changes) -> Settings:
    return Settings.model_construct(**(dict.fromkeys(FLAGS, True) | changes))


def test_the_switch_is_off_unless_the_operator_sets_it(monkeypatch):
    monkeypatch.delenv("SYNC_SCHEMA_V2_ENABLED", raising=False)
    assert Settings(_env_file=None).sync_schema_v2_enabled is False
    assert SCHEMA_V2_CAPABILITY not in REQUIRED_SYNC_CAPABILITIES


def test_switch_off_leaves_every_switch_combination_unchanged():
    """The previous document is the current one without the new switch: same bytes."""
    for values in itertools.product((False, True), repeat=len(FLAGS)):
        flags = dict(zip(FLAGS, values, strict=True))
        off = sync_configuration(Settings.model_construct(**flags, sync_schema_v2_enabled=False))
        assert SCHEMA_V2_CAPABILITY not in off.capabilities
        assert (off.schema_version, off.schema_hash) == (1, SCHEMA_V1.schema_hash)


def test_switch_on_appends_the_capability_last_and_advertises_version_two(monkeypatch):
    body = discovery(monkeypatch, **dict.fromkeys(FLAGS, True), sync_schema_v2_enabled=True)
    assert body == compact(CONTRACT_V2["configuration"])
    assert json.loads(body)["capabilities"][-1] == SCHEMA_V2_CAPABILITY


def test_switch_on_with_the_previous_releases_switches_gives_the_frozen_bytes(monkeypatch):
    flags = dict.fromkeys(FLAGS, True) | {"sync_publication_usage_stats_enabled": False}
    body = discovery(monkeypatch, **flags, sync_schema_v2_enabled=True)
    assert len(body) == 594
    assert hashlib.sha256(body).hexdigest() == V0186_ALL_FLAGS_SWITCH_ON_SHA256
    off = discovery(monkeypatch, **flags, sync_schema_v2_enabled=False)
    assert off == V0186_ALL_FLAGS


@pytest.mark.parametrize(
    "flag",
    [
        "oidc_enabled",
        "sync_integrations_enabled",
        "sync_credentials_enabled",
        "sync_runs_enabled",
        "mcp_public_endpoints_enabled",
    ],
)
def test_the_capability_and_pair_follow_the_feed_capability(flag):
    configured = sync_configuration(settings(**{flag: False}, sync_schema_v2_enabled=True))
    assert "sync.feeds.v1" not in configured.capabilities
    assert SCHEMA_V2_CAPABILITY not in configured.capabilities
    assert (configured.schema_version, configured.schema_hash) == (1, SCHEMA_V1.schema_hash)
    assert new_installation_schema(settings(**{flag: False}, sync_schema_v2_enabled=True)) is (
        SCHEMA_V1
    )
    assert new_installation_schema(settings(sync_schema_v2_enabled=True)) is SCHEMA_V2
    assert new_installation_schema(settings(sync_schema_v2_enabled=False)) is SCHEMA_V1


def test_the_document_model_accepts_version_two_only_up_to_two():
    document = dict(CONTRACT_V2["configuration"])
    assert SyncConfigurationV1.model_validate(document).schema_version == 2
    with pytest.raises(ValidationError):
        SyncConfigurationV1.model_validate({**document, "schema_version": 3})


def test_a_consumer_names_the_pairs_it_accepts():
    v2 = CONTRACT_V2["configuration"]
    with pytest.raises(SyncContractUnsupported):
        require_sync_contract(v2)
    assert require_sync_contract(v2, accepted=(SCHEMA_V1, SCHEMA_V2))
    v1 = sync_configuration(settings()).model_dump()
    assert require_sync_contract(v1, accepted=(SCHEMA_V1, SCHEMA_V2))
    with pytest.raises(SyncContractUnsupported):
        require_sync_contract(v1, accepted=(SCHEMA_V2,))


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 1},  # version 2's hash with version 1
        {"schema_hash": SCHEMA_V1.schema_hash},  # version 1's hash with version 2
        {"capabilities": CONTRACT_V2["configuration"]["capabilities"][:-1]},
    ],
)
def test_a_mixed_pair_or_a_missing_capability_is_refused(change):
    payload = {**CONTRACT_V2["configuration"], **change}
    with pytest.raises(SyncContractUnsupported):
        require_sync_contract(payload, accepted=(SCHEMA_V1, SCHEMA_V2))


def test_version_one_with_the_capability_is_refused():
    payload = sync_configuration(settings()).model_dump()
    payload["capabilities"].append(SCHEMA_V2_CAPABILITY)
    with pytest.raises(SyncContractUnsupported):
        require_sync_contract(payload, accepted=(SCHEMA_V1, SCHEMA_V2))


def test_the_version_two_fixture_records_are_valid_under_version_two_only():
    for kind, record in CONTRACT_V2["records"].items():
        parsed = FeedRecord.model_validate_json(json.dumps(record))
        assert parsed.public_payload(kind, SCHEMA_V2)
        if kind != "faq":
            with pytest.raises(ValueError):
                parsed.public_payload(kind, SCHEMA_V1)


def test_the_capability_is_listed_in_the_rollout_doc():
    from tests.api.test_sync_contract import _rollout_capabilities

    advertised = sync_configuration(settings(sync_schema_v2_enabled=True)).capabilities
    assert set(advertised) - _rollout_capabilities() == set()


# ----------------------------------------------------------------------------
# The upgrade route
# ----------------------------------------------------------------------------

UPGRADE = "/api/v1/workspaces/{wid}/sync-installations/{iid}/schema/upgrade"
BODY = {
    "target_schema_version": 2,
    "expected": {"schema_version": 1, "schema_hash": SCHEMA_V1.schema_hash, "generation": 1},
}


@pytest.mark.parametrize(
    "flags",
    [
        dict.fromkeys(FLAGS, True) | {"sync_schema_v2_enabled": False},
        dict.fromkeys(FLAGS, True)
        | {"sync_schema_v2_enabled": True, "sync_credentials_enabled": False},
        dict.fromkeys(FLAGS, False) | {"sync_schema_v2_enabled": True},
    ],
)
def test_the_upgrade_answers_404_before_authentication_while_not_advertised(monkeypatch, flags):
    configured = Settings.model_construct(**flags)
    monkeypatch.setattr(sync_records, "get_settings", lambda: configured)
    from services import sync_feed_authority

    monkeypatch.setattr(sync_feed_authority, "get_settings", lambda: configured)
    app = FastAPI()
    app.include_router(sync_records.router, prefix="/api/v1")
    response = TestClient(app).post(UPGRADE.format(wid=uuid4(), iid=uuid4()), json=BODY)
    assert response.status_code == 404
    assert response.content == b'{"error":"not_found"}'
    assert response.headers["cache-control"] == "no-store"


def test_the_upgrade_route_is_documented():
    from api.main import app

    path = app.openapi()["paths"][
        UPGRADE.replace("{wid}", "{workspace_id}").replace("{iid}", "{installation_id}")
    ]["post"]
    body = path["requestBody"]["content"]["application/json"]["schema"]
    assert body["required"] == ["target_schema_version", "expected"]
    assert body["additionalProperties"] is False
    result = path["responses"]["200"]["content"]["application/json"]["schema"]
    assert result == {"$ref": "#/components/schemas/SchemaUpgradeResult"}


@pytest.mark.parametrize(
    "change",
    [
        {"target_schema_version": 1},
        {"target_schema_version": 3},
        {"target_schema_version": "2"},
        {"target_schema_version": 2.0},
        {"target_schema_version": True},
        {"target_schema_version": None},
        {"extra": 1},
        {"expected": None},
        {"expected": {**BODY["expected"], "schema_hash": SCHEMA_V1.schema_hash.upper()}},
        {"expected": {**BODY["expected"], "generation": 0}},
        {"expected": {**BODY["expected"], "generation": 2**31}},
        {"expected": {**BODY["expected"], "schema_version": 0}},
        {"expected": {**BODY["expected"], "unknown": 1}},
        {"expected": {"schema_version": 1, "schema_hash": SCHEMA_V1.schema_hash}},
    ],
)
def test_the_upgrade_body_is_strict(change):
    with pytest.raises(ValidationError):
        SchemaUpgrade.model_validate_json(json.dumps({**BODY, **change}))


def test_a_valid_upgrade_body_parses():
    body = SchemaUpgrade.model_validate_json(json.dumps(BODY))
    assert (body.target_schema_version, body.expected.generation) == (2, 1)
