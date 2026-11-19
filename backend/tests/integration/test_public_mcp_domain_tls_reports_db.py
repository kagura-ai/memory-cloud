"""Edge certificate reports on real OIDC and PostgreSQL (#2941).

The reporter's ``PUT /internal/public-mcp-domains/tls-observation`` and the
owner's installation-bound observation that shows a report: the storage (one
row per claim, replaced only by a newer report, written without locking the
domain row), the read only while the feature is configured, the deadline the
evidence puts on the observation, and the maintenance pass that deletes
expired reports. Every test uses its own host names and deletes its rows.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, event, select, text

from api.routes import internal_public_mcp_domains
from config.settings import get_settings
from models.public_mcp_domain import PublicMcpDomain
from models.public_mcp_domain_tls import PublicMcpDomainTlsReport
from services import public_mcp_domain_maintenance as maintenance
from services import public_mcp_domains as lifecycle
from tests.integration import sync_installation_fixtures as fixtures
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store

ROUTE = "/internal/public-mcp-domains/tls-observation"
TABLE = "public_mcp_domain_tls_reports"
TOKEN = "reporter-" + "k" * 40


def name(label: str = "edge") -> str:
    return f"{label}-{uuid4().hex[:10]}.example.org"


def iso(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def now() -> datetime:
    return datetime.now(UTC)


@pytest_asyncio.fixture(loop_scope="session")
async def edge(store, monkeypatch, async_engine):
    settings = get_settings()
    for key, value in {
        "mcp_public_usage_budget_enabled": True,
        "mcp_public_domains_enabled": True,
        "mcp_public_domains_routing_target": "edge.example.com",
        "mcp_public_domains_tls_reporter_token": TOKEN,
    }.items():
        monkeypatch.setattr(settings, key, value)
    store.app.include_router(internal_public_mcp_domains.router)
    await fixtures.paid(store)
    approval = await fixtures.approve(store, fixtures.approval_body(application_id=str(uuid4())))
    activated = await fixtures.http(
        store, "POST", "sync-installations", body=fixtures.activation(approval)
    )
    assert activated.status_code == 201, activated.text
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield SimpleNamespace(
            store=store,
            target=activated.json(),
            statements=statements,
            monkeypatch=monkeypatch,
            engine=async_engine,
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)
        async with store.factory() as db:
            ids = (
                await db.scalars(
                    select(PublicMcpDomain.id).where(
                        PublicMcpDomain.workspace_id.in_([store.wid, store.other])
                    )
                )
            ).all()
            await db.execute(
                delete(PublicMcpDomainTlsReport).where(PublicMcpDomainTlsReport.domain_id.in_(ids))
            )
            await db.execute(
                delete(PublicMcpDomain).where(
                    PublicMcpDomain.workspace_id.in_([store.wid, store.other])
                )
            )
            await db.commit()


async def held(
    edge,
    *,
    state: str = "active",
    created: timedelta = timedelta(minutes=10),
    endpoint: UUID | None = None,
):
    """A domain row (by default of the installation's publication), created ``created`` ago."""
    at = utcnow() - created
    row = PublicMcpDomain(
        id=uuid4(),
        workspace_id=edge.store.wid,
        endpoint_id=endpoint or UUID(edge.target["endpoint_id"]),
        hostname=name(),
        state=state,
        verification_token="a" * 64,
        created_at=at,
        verified_at=at if state != "pending" else None,
        proof_ok_at=at if state != "pending" else None,
        last_checked_at=at,
        last_proof_result="ok",
        routing_status="ok",
        serve_until=utcnow() + timedelta(hours=1) if state == "active" else None,
        suspended_reason="proof_lost" if state == "suspended" else None,
        suspended_at=at if state == "suspended" else None,
    )
    async with edge.store.factory() as db:
        db.add(row)
        await db.commit()
    return row


def report(row, **tls: Any) -> dict:
    observed = tls.pop("observed_at", now() - timedelta(seconds=5))
    body = {
        "status": "valid",
        "observed_at": iso(observed),
        "valid_until": iso(observed + timedelta(minutes=3)),
        "certificate_not_after": iso(observed + timedelta(days=30)),
    }
    body.update({k: (iso(v) if isinstance(v, datetime) else v) for k, v in tls.items()})
    return {
        "domain_id": str(row.id),
        "hostname": row.hostname,
        "endpoint_id": str(row.endpoint_id),
        "tls": body,
    }


async def put(edge, body: Any, *, token: str | None = TOKEN, raw: bytes | None = None) -> Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with AsyncClient(
        transport=ASGITransport(app=edge.store.app), base_url="https://test"
    ) as client:
        if raw is not None:
            headers["Content-Type"] = "application/json"
            return await client.put(ROUTE, content=raw, headers=headers)
        return await client.put(ROUTE, json=body, headers=headers)


async def observe(edge) -> dict:
    path = f"sync-installations/{edge.target['installation_id']}/publication-domains"
    response = await fixtures.http(edge.store, "GET", path)
    assert response.status_code == 200, response.text
    return response.json()


async def stored(edge, domain_id) -> PublicMcpDomainTlsReport | None:
    async with edge.store.factory() as db:
        return await db.get(PublicMcpDomainTlsReport, domain_id)


@contextmanager
def recorded(edge) -> Iterator[list[str]]:
    start = len(edge.statements)
    seen: list[str] = []
    yield seen
    seen.extend(edge.statements[start:])


def on_table(statements: list[str]) -> list[str]:
    return [s for s in statements if TABLE in s]


# ----------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------


async def test_a_report_is_one_row_per_claim_replaced_only_by_a_newer_one(edge):
    row = await held(edge)
    first = report(row, observed_at=now() - timedelta(seconds=30))
    assert (await put(edge, first)).status_code == 204
    assert (await put(edge, first)).status_code == 204  # the exact retry
    async with edge.store.factory() as db:
        count = await db.scalar(
            text("SELECT count(*) FROM public_mcp_domain_tls_reports WHERE domain_id = :id"),
            {"id": row.id},
        )
    assert count == 1

    conflicting = report(row, observed_at=now() - timedelta(seconds=30))
    conflicting["tls"]["observed_at"] = first["tls"]["observed_at"]
    conflicting["tls"]["status"] = "renewal_failed"
    assert (await put(edge, conflicting)).status_code == 409
    older = report(row, observed_at=now() - timedelta(seconds=60))
    assert (await put(edge, older)).status_code == 409
    kept = await stored(edge, row.id)
    assert kept is not None and (kept.status, iso(kept.observed_at)) == (
        "valid",
        first["tls"]["observed_at"],
    )

    newer = report(row, status="renewal_failed", observed_at=now() - timedelta(seconds=10))
    assert (await put(edge, newer)).status_code == 204
    replaced = await stored(edge, row.id)
    assert replaced is not None
    assert (replaced.status, iso(replaced.observed_at), iso(replaced.valid_until)) == (
        "renewal_failed",
        newer["tls"]["observed_at"],
        newer["tls"]["valid_until"],
    )
    assert (replaced.endpoint_id, replaced.hostname) == (row.endpoint_id, row.hostname)


async def test_the_write_never_locks_the_domain_row(edge):
    """A report is stored while another transaction holds the domain row exclusively."""
    row = await held(edge)
    holder = edge.store.factory()
    try:
        await holder.execute(
            select(PublicMcpDomain).where(PublicMcpDomain.id == row.id).with_for_update()
        )
        response = await asyncio.wait_for(put(edge, report(row)), 5)
        assert response.status_code == 204
    finally:
        await holder.rollback()
        await holder.close()
    with recorded(edge) as seen:
        assert (
            await put(edge, report(row, observed_at=now() - timedelta(seconds=1)))
        ).status_code == 204
    writes = on_table(seen)
    assert len(writes) == 1 and writes[0].lstrip().upper().startswith("INSERT")
    assert "ON CONFLICT" in writes[0].upper()
    domain_reads = [s for s in seen if "FROM public_mcp_domains" in s]
    assert domain_reads and not [s for s in domain_reads if "FOR UPDATE" in s.upper()]
    assert not [s for s in seen if s.lstrip().upper().startswith("UPDATE public_mcp_domains")]


# ----------------------------------------------------------------------------
# Nothing touches the table while the feature is not configured
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "switch",
    [
        ("mcp_public_domains_tls_reporter_token", ""),
        ("mcp_public_endpoints_enabled", False),
    ],
)
async def test_nothing_touches_the_report_table_while_not_configured(edge, switch):
    row = await held(edge)
    assert (await put(edge, report(row))).status_code == 204  # stored while configured
    edge.monkeypatch.setattr(get_settings(), *switch)
    with recorded(edge) as seen:
        refused = await put(edge, report(row, observed_at=now()))
        assert refused.status_code == 404
        if switch[0] != "mcp_public_endpoints_enabled":
            observation = await observe(edge)
            assert observation["domain"]["tls"] == {"status": "unreported"}
        async with edge.store.factory() as db:
            await maintenance.housekeeping(db, maintenance.MaintenanceCounts())
    assert seen and on_table(seen) == []


async def test_the_owner_domain_paths_never_touch_the_report_table(edge):
    """Configured or not: the lifecycle and the owner API's domain object never read reports."""
    row = await held(edge)
    assert (await put(edge, report(row))).status_code == 204
    from api.routes.public_mcp_domains import domain_response

    slug = edge.target["resource_id"]
    with recorded(edge) as seen:
        async with edge.store.factory() as db:
            listed = await lifecycle.list_domains(db, edge.store.wid, slug)
            assert [r.id for r in listed] == [row.id]
            await domain_response(db, listed[0])
        async with edge.store.factory() as db:
            await lifecycle.detach_domain(
                db, edge.store.users["owner"], edge.store.wid, slug, row.id
            )
    assert seen and on_table(seen) == []


async def test_the_observation_reads_the_report_once_while_configured(edge):
    row = await held(edge)
    assert (await put(edge, report(row))).status_code == 204
    with recorded(edge) as seen:
        observation = await observe(edge)
    assert observation["domain"]["tls"]["status"] == "valid"
    reads = on_table(seen)
    assert len(reads) == 1 and reads[0].lstrip().upper().startswith("SELECT")
    assert "FOR UPDATE" not in reads[0].upper() and "FOR SHARE" not in reads[0].upper()


# ----------------------------------------------------------------------------
# The observation's deadline
# ----------------------------------------------------------------------------


async def test_shown_evidence_shortens_the_observation_and_passed_evidence_registers_none(edge):
    row = await held(edge)
    soon = now() - timedelta(seconds=2)
    body = report(row, observed_at=soon, valid_until=soon + timedelta(seconds=12))
    assert (await put(edge, body)).status_code == 204
    observation = await observe(edge)
    assert observation["domain"]["tls"]["valid_until"] == body["tls"]["valid_until"]
    assert observation["valid_until"] == body["tls"]["valid_until"]

    # Past its own valid_until: unreported, and the answer is still a 200 with
    # the usual lifetime (no passed deadline is registered).
    async with edge.store.factory() as db:
        await db.execute(
            text(
                "UPDATE public_mcp_domain_tls_reports SET "
                "observed_at = now() - interval '2 minutes', "
                "valid_until = now() - interval '1 second' WHERE domain_id = :id"
            ),
            {"id": row.id},
        )
        await db.commit()
    later = await observe(edge)
    assert later["domain"]["tls"] == {"status": "unreported"}
    lifetime = datetime.fromisoformat(later["valid_until"]) - datetime.fromisoformat(
        later["observed_at"]
    )
    assert timedelta(seconds=25) <= lifetime <= timedelta(seconds=30)


# ----------------------------------------------------------------------------
# Cleanup
# ----------------------------------------------------------------------------


async def test_the_maintenance_pass_deletes_expired_reports_only(edge):
    fresh, old = await held(edge), await held(edge, endpoint=uuid4())
    for row in (fresh, old):
        assert (await put(edge, report(row))).status_code == 204
    async with edge.store.factory() as db:
        await db.execute(
            text(
                "UPDATE public_mcp_domain_tls_reports SET "
                "observed_at = now() - interval '10 minutes', "
                "valid_until = now() - interval '6 minutes' WHERE domain_id = :id"
            ),
            {"id": old.id},
        )
        await db.commit()
    async with edge.store.factory() as db:
        await maintenance.housekeeping(db, maintenance.MaintenanceCounts())
    assert await stored(edge, old.id) is None
    assert await stored(edge, fresh.id) is not None


# ----------------------------------------------------------------------------
# The route: the order of its checks
# ----------------------------------------------------------------------------

INVALID = {"domain_id": "x"}


@pytest.mark.parametrize(
    "switch",
    [
        ("mcp_public_domains_tls_reporter_token", ""),
        ("mcp_public_domains_enabled", False),
        ("mcp_public_endpoints_enabled", False),
    ],
)
async def test_a_report_is_404_while_not_configured_even_with_an_invalid_body(edge, switch):
    edge.monkeypatch.setattr(get_settings(), *switch)
    with recorded(edge) as seen:
        for body in (INVALID, report(nowhere())):
            response = await put(edge, body)
            assert (response.status_code, response.content) == (404, b"")
        assert (await put(edge, None, raw=b"{not json")).status_code == 404
    assert seen == []


def nowhere() -> SimpleNamespace:
    """A report target that names no row (for refusals decided before any read)."""
    return SimpleNamespace(id=uuid4(), hostname=name(), endpoint_id=uuid4())


@pytest.mark.parametrize("token", [None, "", "wrong-" + "w" * 40, TOKEN + "x", TOKEN.upper()])
async def test_a_missing_or_wrong_credential_is_401_even_with_an_invalid_body(edge, token):
    with recorded(edge) as seen:
        for body in (INVALID, report(nowhere())):
            response = await put(edge, body, token=token)
            assert (response.status_code, response.content) == (401, b"")
        assert (await put(edge, None, token=token, raw=b"{not json")).status_code == 401
    assert seen == []


async def test_two_authorization_headers_are_401(edge):
    async with AsyncClient(
        transport=ASGITransport(app=edge.store.app), base_url="https://test"
    ) as client:
        response = await client.put(
            ROUTE,
            json=report(nowhere()),
            headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
        )
    assert response.status_code == 401


async def test_a_body_over_four_kibibytes_is_413_before_it_is_parsed(edge):
    padded = b'{"domain_id": "' + b"a" * 4096 + b'"}'
    with recorded(edge) as seen:
        response = await put(edge, None, raw=padded)
        assert (response.status_code, response.content) == (413, b"")
        exact = b" " * (4096 - 2) + b"{}"  # exactly the limit: parsed (and invalid)
        assert (await put(edge, None, raw=exact)).status_code == 422
    assert seen == []


def _tls(body: dict, **changes: Any) -> dict:
    return {**body, "tls": {**body["tls"], **changes}}


INVALID_BODIES: dict[str, Any] = {
    "tls without its times": lambda b: {**b, "tls": {"status": "valid"}},
    "an empty host name": lambda b: {**b, "hostname": ""},
    "a domain id that is not a uuid": lambda b: {**b, "domain_id": "not-a-uuid"},
    "an unknown key": lambda b: {**b, "source": "edge"},
    "an unknown status": lambda b: _tls(b, status="revoked"),
    "an early time out of range in utc": lambda b: _tls(
        b, certificate_not_after="0001-01-01T00:00:00+05:00"
    ),
    "a late time out of range in utc": lambda b: _tls(b, observed_at="9999-12-31T23:59:59-05:00"),
    "a time without a zone": lambda b: _tls(b, observed_at="2026-11-01T12:00:00"),
    "a window over five minutes": lambda b: _tls(
        b, valid_until=iso(datetime.fromisoformat(b["tls"]["observed_at"]) + timedelta(minutes=6))
    ),
}


@pytest.mark.parametrize("case", sorted(INVALID_BODIES))
async def test_an_invalid_body_is_422_before_any_statement(edge, case):
    body = INVALID_BODIES[case](report(nowhere()))
    with recorded(edge) as seen:
        response = await put(edge, body)
        assert (response.status_code, response.content) == (422, b""), case
    assert seen == []


@pytest.mark.parametrize("field", ["domain_id", "hostname", "endpoint_id"])
async def test_each_identifier_mismatched_on_its_own_is_404(edge, field):
    row = await held(edge)
    body = report(row)
    body[field] = str(uuid4()) if field != "hostname" else name("other")
    response = await put(edge, body)
    assert (response.status_code, response.content) == (404, b"")
    assert await stored(edge, row.id) is None


@pytest.mark.parametrize("state", ["pending", "released"])
async def test_a_claim_that_holds_no_name_is_404(edge, state):
    if state == "released":
        row = await held(edge, state="active")
        async with edge.store.factory() as db:
            await db.execute(
                text(
                    "UPDATE public_mcp_domains SET state = 'released', serve_until = NULL, "
                    "released_at = now(), cooldown_until = now() + interval '1 day' "
                    "WHERE id = :id"
                ),
                {"id": row.id},
            )
            await db.commit()
    else:
        row = await held(edge, state=state)
    response = await put(edge, report(row))
    assert (response.status_code, response.content) == (404, b"")
    assert await stored(edge, row.id) is None


async def test_a_suspended_claim_accepts_and_shows_a_report(edge):
    row = await held(edge, state="suspended")
    body = report(row, status="renewal_failed")
    assert (await put(edge, body)).status_code == 204
    assert (await observe(edge))["domain"]["tls"] == body["tls"]


async def test_a_report_observed_before_the_claim_was_created_is_404(edge):
    row = await held(edge, created=timedelta(seconds=30))
    response = await put(edge, report(row, observed_at=now() - timedelta(seconds=60)))
    assert (response.status_code, response.content) == (404, b"")


async def test_a_report_from_the_future_or_already_expired_is_409(edge):
    row = await held(edge)
    future = report(row, observed_at=now() + timedelta(seconds=60))
    assert (await put(edge, future)).status_code == 409
    past = now() - timedelta(minutes=4)
    expired = report(row, observed_at=past, valid_until=past + timedelta(minutes=3))
    assert (await put(edge, expired)).status_code == 409
    assert await stored(edge, row.id) is None


# ----------------------------------------------------------------------------
# What the observation shows
# ----------------------------------------------------------------------------


async def test_the_observation_shows_a_report_until_its_valid_until(edge):
    from api.routes import sync_publication_domains as observation

    row = await held(edge)
    body = report(row, status="valid")
    assert (await put(edge, body)).status_code == 204
    assert (await observe(edge))["domain"]["tls"] == body["tls"]
    later = utcnow() + timedelta(minutes=4)  # past the report's valid_until
    edge.monkeypatch.setattr(observation, "utcnow", lambda: later)
    assert (await observe(edge))["domain"]["tls"] == {"status": "unreported"}


async def test_a_valid_report_past_its_certificate_end_is_shown_as_expired(edge):
    row = await held(edge)
    observed = now() - timedelta(seconds=30)
    body = report(row, observed_at=observed, certificate_not_after=observed + timedelta(seconds=10))
    assert (await put(edge, body)).status_code == 204
    shown = (await observe(edge))["domain"]["tls"]
    assert shown == {**body["tls"], "status": "expired"}


async def test_a_reassigned_publication_starts_unreported(edge):
    """Release and claim again on the same publication: the old id is refused, the new row
    shows nothing until a report for it, observed after it was created, arrives."""
    old = await held(edge)
    assert (await put(edge, report(old))).status_code == 204
    async with edge.store.factory() as db:
        await db.execute(
            text(
                "UPDATE public_mcp_domains SET state = 'released', serve_until = NULL, "
                "released_at = now(), cooldown_until = now() + interval '1 day' WHERE id = :id"
            ),
            {"id": old.id},
        )
        await db.commit()
    new = await held(edge, created=timedelta(seconds=20))
    assert (await put(edge, report(old, observed_at=now()))).status_code == 404
    assert (await observe(edge))["domain"]["domain_id"] == str(new.id)
    assert (await observe(edge))["domain"]["tls"] == {"status": "unreported"}
    # Observed before the new claim existed: not its evidence.
    early = report(new, observed_at=now() - timedelta(seconds=40))
    assert (await put(edge, early)).status_code == 404
    fresh = report(new)
    assert (await put(edge, fresh)).status_code == 204
    assert (await observe(edge))["domain"]["tls"] == fresh["tls"]


# ----------------------------------------------------------------------------
# The consumer's shapes, and what evidence leaves unchanged
# ----------------------------------------------------------------------------

UTC_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
TOP_KEYS = {
    "schema_version",
    "subject",
    "workspace_id",
    "installation_id",
    "application_id",
    "resource_id",
    "resource_pk",
    "context_id",
    "endpoint_id",
    "public_id",
    "generation",
    "observed_at",
    "valid_until",
    "domain",
}
DOMAIN_KEYS = {
    "domain_id",
    "hostname",
    "url",
    "state",
    "suspended_reason",
    "serving",
    "verification",
    "routing",
    "last_check",
    "proof_ok_at",
    "proof_missing_checks",
    "serve_until",
    "created_at",
    "verified_at",
    "tls",
}
TLS_KEYS = {"status", "observed_at", "valid_until", "certificate_not_after"}


def _instants(observation: dict) -> list[str]:
    domain = observation["domain"]
    values = [observation["observed_at"], observation["valid_until"]]
    values += [domain[k] for k in ("proof_ok_at", "serve_until", "created_at", "verified_at")]
    values.append(domain["last_check"]["checked_at"])
    values += [
        domain["tls"].get(k) for k in ("observed_at", "valid_until", "certificate_not_after")
    ]
    return [value for value in values if value is not None]


async def test_evidence_in_another_offset_keeps_the_consumers_exact_shape(edge):
    row = await held(edge)
    tokyo = timezone(timedelta(hours=9))
    observed = (now() - timedelta(seconds=5)).astimezone(tokyo)
    body = report(row)
    body["tls"] = {
        "status": "valid",
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(minutes=3)).isoformat(),
        "certificate_not_after": (observed + timedelta(days=30)).isoformat(),
    }
    assert body["tls"]["observed_at"].endswith("+09:00")
    assert (await put(edge, body)).status_code == 204
    observation = await observe(edge)
    assert set(observation) == TOP_KEYS
    domain = observation["domain"]
    assert set(domain) == DOMAIN_KEYS
    assert set(domain["verification"]) == {"record_type", "record_name", "record_value"}
    assert set(domain["routing"]) == {"target", "status"}
    assert set(domain["last_check"]) == {"result", "checked_at"}
    assert set(domain["tls"]) == TLS_KEYS
    assert observation["schema_version"] == "public-domain-observation-v1"
    for value in _instants(observation):
        assert UTC_INSTANT.match(value), value
    assert domain["tls"]["observed_at"] == iso(observed)


CONSUMER_SHAPES = {
    "unreported": None,
    "pending": {"status": "pending", "certificate_not_after": None},
    "valid": {"status": "valid"},
    "issuance_failed": {"status": "issuance_failed", "certificate_not_after": None},
    "renewal_failed": {"status": "renewal_failed"},
    "expired": {"status": "expired", "certificate_not_after": "ELAPSED"},
}


@pytest.mark.parametrize("shape", sorted(CONSUMER_SHAPES))
async def test_each_shape_the_consumer_accepts(edge, shape):
    row = await held(edge)
    change = CONSUMER_SHAPES[shape]
    if change is not None:
        observed = now() - timedelta(seconds=5)
        if change.get("certificate_not_after") == "ELAPSED":
            change = {**change, "certificate_not_after": observed - timedelta(days=1)}
        assert (await put(edge, report(row, observed_at=observed, **change))).status_code == 204
    tls = (await observe(edge))["domain"]["tls"]
    if shape == "unreported":
        assert tls == {"status": "unreported"}
        return
    assert set(tls) == TLS_KEYS and tls["status"] == shape
    for key in ("observed_at", "valid_until"):
        assert UTC_INSTANT.match(tls[key])
    if change.get("certificate_not_after", "set") is None:
        assert tls["certificate_not_after"] is None
    else:
        assert UTC_INSTANT.match(tls["certificate_not_after"])


async def _surfaces(edge, row) -> dict:
    """What a report must never change: the standard endpoint, the alias, the
    permission call, the owner API's domain object and the paid right."""
    from api.routes.public_mcp_domains import domain_response
    from mcp_server.public.publication import (
        load_serving_endpoint,
        load_serving_endpoint_for_alias,
    )
    from services.integration_entitlements import inspect_paid_integration

    async with edge.store.factory() as db:
        standard = await load_serving_endpoint(db, edge.target["public_id"])
        alias = await load_serving_endpoint_for_alias(db, row.hostname)
        current = await db.get(PublicMcpDomain, row.id)
        assert current is not None
        owner_view = (await domain_response(db, current)).model_dump(mode="json")
        right = await inspect_paid_integration(db, edge.store.wid, edge.store.service_id)
    async with AsyncClient(
        transport=ASGITransport(app=edge.store.app), base_url="https://test"
    ) as client:
        permission = await client.get(
            "/internal/public-mcp-domains/tls-permission", params={"domain": row.hostname}
        )
    return {
        "standard": standard.id if standard else None,
        "alias": alias.id if alias else None,
        "permission": (permission.status_code, permission.content),
        "owner_view": owner_view,
        "right": (right.allowed, right.reason, right.revision, right.valid_until),
    }


async def test_the_standard_endpoint_permissions_and_rights_are_the_same_with_evidence(edge):
    row = await held(edge)
    before = await _surfaces(edge, row)
    assert before["standard"] is not None and before["alias"] is not None
    assert before["permission"] == (200, b"")
    assert (await put(edge, report(row, status="renewal_failed"))).status_code == 204
    assert await _surfaces(edge, row) == before
