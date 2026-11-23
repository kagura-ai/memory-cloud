"""#2979: the CNAME proof fields of a custom domain answer, decided from the setting.

``verification.cname_target`` and ``last_check.proof`` are present (``proof``
may be null) only while ``MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE`` is set; with
it empty the answer has exactly the keys it had before, whatever the row's
stored column holds. The sync observation (``LiveDomain``, rebuilt from
``model_dump()``) keeps the same omission. No database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api.routes.public_mcp_domains import domain_response
from api.routes.sync_publication_domains import LiveDomain, PublicationDomainObservation
from config.settings import get_settings
from mcp_server.public.domains import cname_proof_target
from services.domain_tls_observations import CertificateObservation

TOKEN = "0f" * 32
ZONE = "proof.example.net"


def _row(**overrides):
    now = datetime(2026, 11, 23, tzinfo=UTC)
    row = {
        "id": uuid4(),
        "hostname": "mcp.customer.example.org",
        "endpoint_id": uuid4(),
        "state": "pending",
        "suspended_reason": None,
        "verification_token": TOKEN,
        "routing_status": "ok",
        "last_proof_result": "ok",
        "last_proof_via": "cname",
        "last_checked_at": now,
        "proof_ok_at": now,
        "proof_missing_checks": 0,
        "serve_until": None,
        "created_at": now,
        "verified_at": None,
    }
    row.update(overrides)
    return SimpleNamespace(**row)


@pytest.fixture
def zone(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_public_domains_routing_target", "edge.example.net")

    def _set(value: str) -> None:
        monkeypatch.setattr(settings, "mcp_public_domains_cname_proof_zone", value)

    return _set


async def _body(row) -> dict:
    response = await domain_response(AsyncMock(), row)
    return response.model_dump(mode="json")


async def test_zone_empty_keeps_the_previous_keys_whatever_the_column_holds(zone) -> None:
    zone("")
    body = await _body(_row(last_proof_via="both"))
    assert set(body["verification"]) == {"record_type", "record_name", "record_value"}
    assert set(body["last_check"]) == {"result", "checked_at"}


async def test_zone_set_adds_the_target_and_the_kind(zone) -> None:
    zone(ZONE)
    body = await _body(_row())
    assert body["verification"]["cname_target"] == cname_proof_target(TOKEN)
    assert body["last_check"]["proof"] == "cname"
    pending = await _body(_row(last_proof_result=None, last_proof_via=None, last_checked_at=None))
    assert pending["last_check"] == {"result": None, "checked_at": None, "proof": None}


@pytest.mark.parametrize("value", ["", ZONE])
async def test_the_sync_observation_keeps_the_same_keys(zone, value: str) -> None:
    zone(value)
    live = await domain_response(AsyncMock(), _row())
    domain = LiveDomain(**live.model_dump(), tls=CertificateObservation())
    now = datetime(2026, 11, 23, tzinfo=UTC)
    observation = PublicationDomainObservation(
        subject="owner",
        workspace_id=uuid4(),
        installation_id=uuid4(),
        application_id="app",
        resource_id="faq",
        resource_pk=uuid4(),
        context_id=uuid4(),
        endpoint_id=uuid4(),
        public_id="pmcp_x",
        generation=1,
        observed_at=now,
        valid_until=now,
        domain=domain,
    )
    body = observation.model_dump(mode="json")["domain"]
    if value:
        assert body["verification"]["cname_target"] == cname_proof_target(TOKEN)
        assert body["last_check"]["proof"] == "cname"
    else:
        assert set(body["verification"]) == {"record_type", "record_name", "record_value"}
        assert set(body["last_check"]) == {"result", "checked_at"}
    assert body["tls"] == {"status": "unreported"}


def test_the_omission_needs_no_field_exclude_if() -> None:
    """Fix round 1: ``Field(exclude_if=...)`` needs a newer pydantic than the
    declared floor; the two small models omit their keys themselves."""
    from api.routes.public_mcp_domains import (
        PublicMcpDomainLastCheck,
        PublicMcpDomainVerification,
    )

    for model, key in (
        (PublicMcpDomainVerification, "cname_target"),
        (PublicMcpDomainLastCheck, "proof"),
    ):
        assert getattr(model.model_fields[key], "exclude_if", None) is None
        schema = model.model_json_schema(mode="serialization")
        assert key in schema["properties"]
        assert "exclude_if" not in repr(schema)


async def test_json_mode_and_python_mode_omit_alike(zone) -> None:
    zone("")
    response = await domain_response(AsyncMock(), _row())
    assert "cname_target" not in response.model_dump()["verification"]
    assert "proof" not in response.model_dump()["last_check"]
    assert '"cname_target"' not in response.model_dump_json()
    zone(ZONE)
    response = await domain_response(AsyncMock(), _row())
    assert response.model_dump()["verification"]["cname_target"] == cname_proof_target(TOKEN)
    assert '"proof":"cname"' in response.model_dump_json()
