"""Golden bytes of the sync documents a strict consumer copies (#3055).

A consumer may hold a byte-exact copy of the discovery document and of the
worker ``/schema`` answer. These tests pin the bytes, not the parsed values:
key order, separators and the absence of a trailing newline are part of the
contract. The ``/schema`` answers are pinned against a real installation in
``tests/integration/test_sync_wire_golden_db.py``; the fixture files
``tests/fixtures/sync-schema-v1.json`` and ``sync-schema-v2.json`` hold the
exact bytes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import well_known
from config.settings import Settings

FIXTURES = Path(__file__).parents[1] / "fixtures"
CONTRACT_V1 = json.loads((FIXTURES / "sync-contract-v1.json").read_text())
# Every deployment switch that shapes the discovery document today.
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
    "sync_publication_domain_operations_enabled",
    "sync_publication_text_operations_enabled",
    "sync_publication_usage_stats_enabled",
)
# The document of a deployment with every switch of v0.186.0 on (577 bytes).
V0186_ALL_FLAGS = (
    b'{"protocol":"kmc.sync.v1","schema_version":1,'
    b'"schema_hash":"bdb55d3bdbbbb72a92a33fb96e6a2dc60ccbaf5b09638dcc1f1619d016dfec0d",'
    b'"capabilities":["identity.oidc.v1","workspace.access.v1","sync.installations.v1",'
    b'"sync.credentials.v1","sync.feeds.v1","sync.managed.v1","sync.activity.v1",'
    b'"public.mcp.v1","public.budget.v1","public.domains.v1",'
    b'"public.domains.observation.v1","public.domains.operations.v1",'
    b'"public.text.operations.v1"],'
    b'"human_scopes":["openid","workspaces:read","integrations:manage","sync.records:read"],'
    b'"service_scope":"resource.sync","assertion_audience":"issuer"}'
)
V0186_ALL_FLAGS_SHA256 = "f0f60fb62f5464a47dd89789559601326342b5d0a5c9a561fedb41a855c81c0c"


def compact(value: object) -> bytes:
    """The serialization of a JSON answer: compact, UTF-8, no trailing newline."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def discovery(monkeypatch, **flags: bool) -> bytes:
    configured = Settings.model_construct(**flags)
    monkeypatch.setattr(well_known, "get_settings", lambda: configured)
    app = FastAPI()
    app.include_router(well_known.router, prefix="/.well-known")
    response = TestClient(app).get("/.well-known/kmc-sync-configuration")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    return response.content


def test_every_switch_on_gives_the_consumer_fixture_byte_for_byte(monkeypatch):
    body = discovery(monkeypatch, **dict.fromkeys(FLAGS, True))
    assert body == compact(CONTRACT_V1["configuration"])


def test_the_previous_releases_switches_give_its_exact_document(monkeypatch):
    flags = dict.fromkeys(FLAGS, True) | {"sync_publication_usage_stats_enabled": False}
    body = discovery(monkeypatch, **flags)
    assert body == V0186_ALL_FLAGS
    assert len(body) == 577
    assert hashlib.sha256(body).hexdigest() == V0186_ALL_FLAGS_SHA256


def test_a_deployment_with_every_switch_off_advertises_nothing(monkeypatch):
    body = discovery(monkeypatch, **dict.fromkeys(FLAGS, False))
    assert body == (
        b'{"protocol":"kmc.sync.v1","schema_version":1,'
        b'"schema_hash":"bdb55d3bdbbbb72a92a33fb96e6a2dc60ccbaf5b09638dcc1f1619d016dfec0d",'
        b'"capabilities":[],'
        b'"human_scopes":["openid","workspaces:read","integrations:manage","sync.records:read"],'
        b'"service_scope":"resource.sync","assertion_audience":"issuer"}'
    )


def test_the_version_one_schema_fixture_is_the_exact_previous_answer():
    body = (FIXTURES / "sync-schema-v1.json").read_bytes()
    assert len(body) == 1864 and not body.endswith(b"\n")
    assert hashlib.sha256(body).hexdigest() == (
        "936fd4e0746941f8db3500cb5a91b6252a77b559d9bd48cb41eb36a6f95c6f32"
    )
    assert compact(json.loads(body)) == body
