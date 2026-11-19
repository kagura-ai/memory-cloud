"""The rules that bind an edge certificate report to one claim (#2941), without a database.

Each read-time rule has its own test: the report names another claim (id, host
name or endpoint), the claim holds no name (pending, released), the report was
observed before the claim was created or after the observation, or it is past
its ``valid_until``; ``valid`` past its certificate end is ``expired`` and
``valid`` without an end is ``unreported``; every other status is shown as
reported. The request body's own rules follow.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.public_mcp_domain import PublicMcpDomain
from models.public_mcp_domain_tls import PublicMcpDomainTlsReport
from services import domain_tls_observations as observations
from services.domain_tls_observations import (
    CertificateObservation,
    DomainTLSReport,
    ObservedCertificate,
    current_certificate,
)

CREATED = datetime(2026, 11, 1, 12, 0, 0)  # naive UTC, as the database stores it
SEEN = CREATED + timedelta(minutes=10)  # the observation's own time (naive UTC)
OBSERVED = (SEEN - timedelta(seconds=20)).replace(tzinfo=UTC)
UNREPORTED = {"status": "unreported"}


def _row(state: str = "active") -> PublicMcpDomain:
    return PublicMcpDomain(
        id=uuid4(),
        endpoint_id=uuid4(),
        hostname="mcp.example.org",
        state=state,
        created_at=CREATED,
    )


def _report(row: PublicMcpDomain, **changes) -> PublicMcpDomainTlsReport:
    values = {
        "domain_id": row.id,
        "endpoint_id": row.endpoint_id,
        "hostname": row.hostname,
        "status": "valid",
        "observed_at": OBSERVED,
        "valid_until": OBSERVED + timedelta(minutes=3),
        "certificate_not_after": OBSERVED + timedelta(days=30),
    }
    values.update(changes)
    return PublicMcpDomainTlsReport(**values)


def _shown(row: PublicMcpDomain, report: PublicMcpDomainTlsReport, at: datetime = SEEN) -> dict:
    return current_certificate(row, report, at).model_dump(mode="json")


def test_a_report_of_the_row_is_shown_with_utc_times():
    row = _row()
    assert _shown(row, _report(row)) == {
        "status": "valid",
        "observed_at": "2026-11-01T12:09:40Z",
        "valid_until": "2026-11-01T12:12:40Z",
        "certificate_not_after": "2026-12-01T12:09:40Z",
    }


def test_no_report_is_unreported():
    assert current_certificate(_row(), None, SEEN) == CertificateObservation()


def test_a_report_of_another_domain_id_is_unreported():
    row = _row()
    assert _shown(row, _report(row, domain_id=uuid4())) == UNREPORTED


def test_a_report_of_another_host_name_is_unreported():
    row = _row()
    assert _shown(row, _report(row, hostname="other.example.org")) == UNREPORTED


def test_a_report_of_another_endpoint_is_unreported():
    row = _row()
    assert _shown(row, _report(row, endpoint_id=uuid4())) == UNREPORTED


@pytest.mark.parametrize("state", ["pending", "released"])
def test_a_claim_that_holds_no_name_shows_nothing(state):
    row = _row(state)
    assert _shown(row, _report(row)) == UNREPORTED


def test_a_suspended_claim_keeps_its_evidence():
    row = _row("suspended")
    assert _shown(row, _report(row))["status"] == "valid"


def test_a_report_observed_before_the_claim_was_created_is_unreported():
    row = _row()
    before = CREATED.replace(tzinfo=UTC) - timedelta(seconds=1)
    report = _report(row, observed_at=before, valid_until=before + timedelta(hours=1))
    assert _shown(row, report) == UNREPORTED
    at_creation = CREATED.replace(tzinfo=UTC)
    report = _report(row, observed_at=at_creation, valid_until=at_creation + timedelta(hours=1))
    assert _shown(row, report)["status"] == "valid"


def test_a_report_observed_after_the_observation_is_unreported():
    row = _row()
    later = SEEN.replace(tzinfo=UTC) + timedelta(seconds=1)
    assert _shown(row, _report(row, observed_at=later)) == UNREPORTED
    at = SEEN.replace(tzinfo=UTC)
    assert _shown(row, _report(row, observed_at=at))["status"] == "valid"


def test_a_report_past_its_valid_until_is_unreported():
    row = _row()
    report = _report(row, valid_until=SEEN.replace(tzinfo=UTC))
    assert _shown(row, report) == UNREPORTED


def test_a_report_within_the_margin_of_its_valid_until_is_unreported():
    """Shown only while valid_until is more than the margin ahead: a deadline it
    registers is still ahead when the observation commits and when it arrives."""
    row = _row()
    edge = SEEN.replace(tzinfo=UTC) + timedelta(seconds=observations.SHOW_MARGIN_SECONDS)
    assert observations.SHOW_MARGIN_SECONDS == 5
    assert _shown(row, _report(row, valid_until=edge)) == UNREPORTED
    just_outside = _report(row, valid_until=edge + timedelta(microseconds=1))
    assert _shown(row, just_outside)["status"] == "valid"


def test_valid_past_the_certificate_end_is_shown_as_expired():
    row = _row()
    report = _report(row, certificate_not_after=SEEN.replace(tzinfo=UTC))
    shown = _shown(row, report)
    assert shown["status"] == "expired"
    assert shown["certificate_not_after"] == "2026-11-01T12:10:00Z"


def test_valid_within_the_margin_of_its_certificate_end_is_shown_as_expired():
    row = _row()
    edge = SEEN.replace(tzinfo=UTC) + timedelta(seconds=observations.SHOW_MARGIN_SECONDS)
    assert _shown(row, _report(row, certificate_not_after=edge))["status"] == "expired"
    just_outside = _report(row, certificate_not_after=edge + timedelta(microseconds=1))
    assert _shown(row, just_outside)["status"] == "valid"


def test_valid_without_a_certificate_end_is_unreported():
    row = _row()
    assert _shown(row, _report(row, certificate_not_after=None)) == UNREPORTED


@pytest.mark.parametrize(
    ("status", "not_after"),
    [
        ("pending", None),
        ("issuance_failed", None),
        ("renewal_failed", OBSERVED + timedelta(days=3)),  # the old certificate is still valid
        ("expired", OBSERVED - timedelta(days=1)),
    ],
)
def test_every_other_status_is_shown_as_reported(status, not_after):
    row = _row()
    shown = _shown(row, _report(row, status=status, certificate_not_after=not_after))
    assert shown["status"] == status
    assert set(shown) == {"status", "observed_at", "valid_until", "certificate_not_after"}


def test_times_in_another_offset_are_shown_in_utc():
    row = _row()
    tokyo = OBSERVED.astimezone(datetime.fromisoformat("2026-01-01T00:00:00+09:00").tzinfo)
    shown = _shown(
        row,
        _report(row, observed_at=tokyo, valid_until=tokyo + timedelta(minutes=3)),
    )
    assert shown["observed_at"] == "2026-11-01T12:09:40Z"
    assert shown["valid_until"].endswith("Z")


# ----------------------------------------------------------------------------
# The request body
# ----------------------------------------------------------------------------


def _body(row: PublicMcpDomain, **tls) -> dict:
    values = {
        "status": "valid",
        "observed_at": "2026-11-01T12:09:40Z",
        "valid_until": "2026-11-01T12:12:40Z",
        "certificate_not_after": "2026-12-01T12:09:40Z",
    }
    values.update(tls)
    return {
        "domain_id": str(row.id),
        "endpoint_id": str(row.endpoint_id),
        "hostname": row.hostname,
        "tls": values,
    }


def _with_state(row: PublicMcpDomain, state: str) -> PublicMcpDomain:
    return PublicMcpDomain(
        id=row.id,
        endpoint_id=row.endpoint_id,
        hostname=row.hostname,
        state=state,
        created_at=row.created_at,
    )


def test_a_well_formed_report_names_a_held_claim_only():
    row = _row()
    report = DomainTLSReport.model_validate(_body(row))
    assert report.matches(row)
    for state in ("pending", "released"):
        assert not report.matches(_with_state(row, state))
    assert report.matches(_with_state(row, "suspended"))


@pytest.mark.parametrize(
    "tls",
    [
        {"valid_until": "2026-11-01T12:14:41Z"},  # more than five minutes
        {"valid_until": "2026-11-01T12:09:40Z"},  # no window
        {"certificate_not_after": None},  # valid without an end
        {"certificate_not_after": "2026-11-01T12:09:40Z"},  # valid with an end already passed
        {"status": "expired", "certificate_not_after": "2026-12-01T12:09:40Z"},
        {"status": "expired", "certificate_not_after": None},
        {"status": "revoked"},
        {"observed_at": "2026-11-01T12:09:40"},  # no time zone
        {"certificate_not_after": "0001-01-01T00:00:00+05:00"},  # out of range in UTC
        {"observed_at": "9999-12-31T23:59:59-05:00"},
        {"issuer": "x"},
    ],
)
def test_a_malformed_report_is_invalid(tls):
    with pytest.raises(ValidationError):
        DomainTLSReport.model_validate(_body(_row(), **tls))


def test_the_body_refuses_unknown_and_missing_keys():
    row = _row()
    with pytest.raises(ValidationError):
        DomainTLSReport.model_validate({**_body(row), "source": "edge"})
    body = _body(row)
    del body["tls"]["certificate_not_after"]
    with pytest.raises(ValidationError):
        DomainTLSReport.model_validate(body)


def test_report_times_are_normalised_to_utc():
    report = DomainTLSReport.model_validate(
        _body(
            _row(),
            observed_at="2026-11-01T21:09:40+09:00",
            valid_until="2026-11-01T21:12:40+09:00",
        )
    )
    assert report.tls.observed_at == datetime(2026, 11, 1, 12, 9, 40, tzinfo=UTC)
    assert ObservedCertificate.model_validate(report.tls.model_dump()).observed_at.tzinfo == UTC


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


@pytest.mark.parametrize(
    "change",
    [
        {"observed_at": _epoch("2026-11-01T12:09:40+00:00")},
        {"valid_until": float(_epoch("2026-11-01T12:12:40+00:00"))},
        {"certificate_not_after": str(_epoch("2026-12-01T12:09:40+00:00"))},
        {"observed_at": str(_epoch("2026-11-01T12:09:40+00:00"))},
    ],
)
def test_report_times_are_rfc3339_strings_only(change):
    """A JSON number or a numeric string is never read as seconds since the epoch."""
    payload = json.dumps(_body(_row(), **change))
    with pytest.raises(ValidationError):
        DomainTLSReport.model_validate_json(payload)
    # The same instants as RFC 3339 strings are a valid report.
    assert DomainTLSReport.model_validate_json(json.dumps(_body(_row())))


def test_the_lifecycle_and_the_owner_api_never_import_the_reports():
    """The domain lifecycle, its command line and the owner API know nothing of reports."""
    src = Path(__file__).resolve().parents[2] / "src"
    forbidden = {"models.public_mcp_domain_tls", "services.domain_tls_observations"}
    for relative in (
        "services/public_mcp_domains.py",
        "cli/public_mcp_domains.py",
        "api/routes/public_mcp_domains.py",
    ):
        imported: set[str] = set()
        for node in ast.walk(ast.parse((src / relative).read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert imported & forbidden == set(), relative
