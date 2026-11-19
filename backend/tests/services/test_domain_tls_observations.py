from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.public_mcp_domain import PublicMcpDomain
from services.domain_tls_observations import (
    DomainTLSReport,
    ObservedCertificate,
    current_certificate,
)


def test_bound_tls_observation_lifetime():
    start = datetime(2026, 11, 1)
    seen = start.replace(tzinfo=UTC)
    row = PublicMcpDomain(
        id=uuid4(),
        endpoint_id=uuid4(),
        hostname="mcp.example.org",
        state="active",
        created_at=start,
    )
    payload = {"domain_id": row.id, "hostname": row.hostname, "endpoint_id": row.endpoint_id}
    tls = {
        "observed_at": seen,
        "valid_until": seen + timedelta(minutes=3),
        "certificate_not_after": seen + timedelta(minutes=1),
    }
    for state in ("pending", "valid", "issuance_failed", "renewal_failed", "expired"):
        evidence = dict(tls, status=state)
        if state == "expired":
            evidence["certificate_not_after"] = seen
        row.tls_report = DomainTLSReport(**payload, tls=evidence).model_dump(mode="json")
        assert current_certificate(row, start).status == state
        assert current_certificate(row, start - timedelta(seconds=1)).status == "unreported"
        assert current_certificate(row, start + timedelta(minutes=3)).status == "unreported"
        if state == "valid":
            expired = current_certificate(row, start + timedelta(minutes=1))
            assert ObservedCertificate.model_validate(expired.model_dump()).status == "expired"
    row.endpoint_id = uuid4()
    assert current_certificate(row, start).status == "unreported"
    row.tls_report = {}
    assert current_certificate(row, start).status == "unreported"
    for patch in (
        {"valid_until": seen + timedelta(minutes=6)},
        {"certificate_not_after": None},
        {"observed_at": start},
        {"certificate_not_after": seen},
    ):
        with pytest.raises(ValidationError):
            DomainTLSReport(**payload, tls=dict(tls, status="valid") | patch)
