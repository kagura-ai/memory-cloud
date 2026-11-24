"""The certificate permission call of a TLS proxy for verified custom domains (#2809).

``GET /internal/public-mcp-domains/tls-permission?domain=<name>`` tells a proxy
that issues certificates on demand whether a name is an alias the application
serves now. It answers with an empty body:

- 200: the alias statement (``load_serving_endpoint_for_alias``) resolves the
  name: an ``active`` row with a current lease, of a live publication of a
  live workspace, with both switches on;
- 404: either switch off, no ``domain`` (or more than one), a name that cannot
  be claimed (malformed, special-use, an application host or reserved), or a
  name that is not served;
- 503: the call cannot be answered now (all lookup slots busy, the lookup
  slower than ``PERMISSION_TIMEOUT_SECONDS``, or any error). A proxy reads
  that as a refusal.

The route carries no credential: a proxy's permission call has none. It lives
under ``/internal``, which the reference proxy answers with 404 from outside
(``deploy.sh verify_internal_blocked`` checks it on every deploy), and the
Host guard answers 404 for every ``/internal`` path under a Host that is not
an application or internal host.

The handler takes no dependency: the switches, the name and the slot are
decided before any database session is opened, and the lookup opens its own
short session inside the time bound. At most ``PERMISSION_CONCURRENCY``
lookups run at once per process, so the call can never hold more of the
database pool than that. Plan pause and capacity lock do not withdraw the
permission; they answer 503 on the alias at request time. No log line carries
the name (``utils.logger`` also removes the query of this path from every
rendered line).

``PUT /internal/public-mcp-domains/tls-observation`` (#2941) is the other route
here: the certificate operator's report about one claim, with its own bearer
credential (``MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN``). It is checked in a fixed
order (not configured, credential, size, body, binding, then freshness and
order) and stored in ``public_mcp_domain_tls_reports`` by
``services.domain_tls_observations``; it never authorizes serving and never
writes the domain row.

``GET /internal/public-mcp-domains/claims`` (#2993) lists the claims an edge
agent must serve or keep (``active`` and ``suspended``), behind the same gate
as the report (not configured 404, the credential 401) and decided before any
session is opened. One read at a time per process, in its own short session
bounded by ``CLAIMS_TIMEOUT_SECONDS``; more than ``CLAIMS_LIMIT`` rows answer
503, never a partial list. The shape is versioned by ``CLAIMS_SCHEMA_VERSION``
(``docs/public-mcp-domains.md``, "Claims list for an edge agent").
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import Select, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.single_authorization import sole_authorization
from config.settings import get_settings
from db.base import get_db
from mcp_server.public.domains import claimable_hostname, public_domains_enabled
from mcp_server.public.publication import (
    load_serving_endpoint_for_alias,
    public_endpoints_enabled,
)
from models.public_mcp_domain import HELD_STATES, PublicMcpDomain
from services.domain_tls_observations import (
    DomainTLSReport,
    reporting_configured,
    store_report,
)
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal-public-mcp-domains"])

PERMISSION_PATH = "/public-mcp-domains/tls-permission"
PERMISSION_CONCURRENCY = 4
PERMISSION_TIMEOUT_SECONDS: float = 3

# Taken only when a slot is free at once (``locked()`` first), so a call never
# waits for one and the semaphore never binds to an event loop.
_SLOTS = asyncio.Semaphore(PERMISSION_CONCURRENCY)


REPORT_PATH = "/public-mcp-domains/tls-observation"
# A report body is far smaller; a larger one is refused before it is parsed.
REPORT_BODY_LIMIT = 4096


REPORT_REFUSED_EVENT = "public_mcp_domain_tls_report_refused"
CLAIMS_REFUSED_EVENT = "public_mcp_domain_claims_refused"


def _refused(
    reason: str,
    status_code: int,
    domain_id: UUID | None = None,
    *,
    event: str = REPORT_REFUSED_EVENT,
) -> Response:
    """A bodyless refusal of a reporter call, logged without the credential or a host name.

    ``disabled`` is a debug line: an installation that does not use reports
    writes no warning for them. A report refusal also names the domain id
    when it is known; a claims refusal carries the reason only.
    """
    log = logger.debug if reason == "disabled" else logger.warning
    if event == REPORT_REFUSED_EVENT:
        log(event, reason=reason, domain_id=str(domain_id) if domain_id else None)
    else:
        log(event, reason=reason)
    return Response(status_code=status_code)


def _reporter_refusal(request: Request, event: str) -> Response | None:
    """The reporter gate of the report and of the claims list, else ``None``.

    Not configured (the credential empty or either switch off): 404. Any
    ``Authorization`` but exactly one ``Bearer <credential>``: 401. It reads
    nothing but the settings and the header, so no statement runs before it.
    """
    settings = get_settings()
    if not reporting_configured(settings):
        return _refused("disabled", 404, event=event)
    token = settings.mcp_public_domains_tls_reporter_token
    authorization = sole_authorization(request) or ""
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        return _refused("credential", 401, event=event)
    return None


async def _bounded(request: Request) -> bytes | None:
    """The body, or ``None`` once it exceeds ``REPORT_BODY_LIMIT``."""
    payload = bytearray()
    async for part in request.stream():
        payload.extend(part)
        if len(payload) > REPORT_BODY_LIMIT:
            return None
    return bytes(payload)


@router.put(REPORT_PATH, include_in_schema=False)
async def receive_tls_observation(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> Response:
    """Accept the reporter's latest certificate report of one claim; never authorize serving.

    Checked in this order, each answered with an empty body: not configured
    (404), the credential (401), the size (413), the body (422), the binding
    to a claim that holds its name (404), then the report's freshness and its
    order against the stored one (409); else stored (204). Nothing of a
    request without the credential is parsed, and no statement runs before
    the binding is read. The domain row is read without a lock and never
    written.
    """
    refusal = _reporter_refusal(request, REPORT_REFUSED_EVENT)
    if refusal is not None:
        return refusal
    payload = await _bounded(request)
    if payload is None:
        return _refused("size", 413)
    try:
        report = DomainTLSReport.model_validate_json(payload)
    except ValidationError:
        return _refused("invalid", 422)
    row = await db.scalar(select(PublicMcpDomain).where(PublicMcpDomain.id == report.domain_id))
    if row is None or not report.matches(row):
        return _refused("binding", 404, report.domain_id)
    now = utcnow().replace(tzinfo=UTC)
    if report.tls.observed_at > now:
        return _refused("future", 409, report.domain_id)
    if now >= report.tls.valid_until:
        return _refused("expired", 409, report.domain_id)
    stored = await store_report(db, report)
    await db.commit()
    if not stored:
        return _refused("older", 409, report.domain_id)
    logger.info(
        "public_mcp_domain_tls_report_stored",
        domain_id=str(report.domain_id),
        status=report.tls.status,
    )
    return Response(status_code=204)


async def _lookup(hostname: str) -> bool:
    """Whether the alias statement serves ``hostname``, from one short session."""
    async with contextlib.aclosing(get_db()) as sessions:
        async for db in sessions:
            return await load_serving_endpoint_for_alias(db, hostname) is not None
    return False  # pragma: no cover - get_db always yields


def _requested_name(request: Request) -> str | None:
    """The one ``domain`` value as a claimable name, else ``None``."""
    values = request.query_params.getlist("domain")
    if len(values) != 1:
        return None
    return claimable_hostname(values[0])


@router.get(PERMISSION_PATH, include_in_schema=False)
async def tls_permission(request: Request) -> Response:
    """Answer a proxy's permission call: 200, 404 or 503, always with an empty body."""
    if not (public_domains_enabled() and public_endpoints_enabled()):
        return Response(status_code=404)
    name = _requested_name(request)
    if name is None:
        return Response(status_code=404)
    if _SLOTS.locked():
        logger.warning("public_mcp_domain_permission_busy")
        return Response(status_code=503)
    async with _SLOTS:
        try:
            served = await asyncio.wait_for(_lookup(name), PERMISSION_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - every failure is the same 503
            logger.warning("public_mcp_domain_permission_failed", error_type=type(exc).__name__)
            return Response(status_code=503)
    return Response(status_code=200 if served else 404)


CLAIMS_PATH = "/public-mcp-domains/claims"
CLAIMS_SCHEMA_VERSION = 1
# More held rows than this answer 503: an edge agent that reconciles its
# records against the list must never see a list cut short.
CLAIMS_LIMIT = 10_000
CLAIMS_TIMEOUT_SECONDS: float = 5

# One claims read per process; taken only when free at once, like ``_SLOTS``.
_CLAIMS_SLOT = asyncio.Semaphore(1)

# The held states as literals, so the statement carries the predicate of the
# partial indexes on held rows (also under a generic plan).
_HELD = PublicMcpDomain.state.in_([literal(state, literal_execute=True) for state in HELD_STATES])


def claims_statement() -> Select[str, str, UUID, UUID, int, datetime]:
    """The one claims read: six columns of the held rows, at most ``CLAIMS_LIMIT + 1``.

    No entity load, no lock and no ``ORDER BY``: a database orders text by its
    collation, so the answer is sorted in byte order after the read (a read
    over the limit is refused whole, so which rows it stopped at never matters).
    """
    return (
        select(
            PublicMcpDomain.hostname,
            PublicMcpDomain.state,
            PublicMcpDomain.id,
            PublicMcpDomain.endpoint_id,
            PublicMcpDomain.check_seq,
            PublicMcpDomain.created_at,
        )
        .where(_HELD)
        .limit(CLAIMS_LIMIT + 1)
    )


async def _held_claims() -> list[dict[str, object]]:
    """The held claims, from one short read-only session.

    The session is closed (its transaction rolled back) before this returns.
    """
    statement = claims_statement()
    async with contextlib.aclosing(get_db()) as sessions:
        async for db in sessions:
            rows = (await db.execute(statement)).all()
            break
        else:  # pragma: no cover - get_db always yields
            rows = []
    return [
        {
            "hostname": row.hostname,
            "state": row.state,
            "domain_id": str(row.id),
            "endpoint_id": str(row.endpoint_id),
            "check_seq": int(row.check_seq),
            "created_at": to_utc_iso(row.created_at),
        }
        for row in rows
    ]


@router.get(CLAIMS_PATH, include_in_schema=False)
async def list_claims(request: Request) -> Response:
    """List the active and suspended claims for the edge agent.

    The reporter gate first (404, 401), then the one claims slot (503 when
    busy), then one read bounded in time and rows (503 on a timeout, an error
    or more than ``CLAIMS_LIMIT`` rows); every refusal has an empty body. Query
    parameters are ignored. No log line carries a host name.
    """
    refusal = _reporter_refusal(request, CLAIMS_REFUSED_EVENT)
    if refusal is not None:
        return refusal
    if _CLAIMS_SLOT.locked():
        logger.warning("public_mcp_domain_claims_busy")
        return Response(status_code=503)
    async with _CLAIMS_SLOT:
        try:
            claims = await asyncio.wait_for(_held_claims(), CLAIMS_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - every failure is the same 503
            logger.warning("public_mcp_domain_claims_failed", error_type=type(exc).__name__)
            return Response(status_code=503)
    if len(claims) > CLAIMS_LIMIT:
        logger.warning("public_mcp_domain_claims_over_limit")
        return Response(status_code=503)
    # Byte (code point) order on every database, whatever its collation.
    claims.sort(key=lambda claim: (str(claim["hostname"]), str(claim["domain_id"])))
    logger.debug("public_mcp_domain_claims_listed", count=len(claims))
    return JSONResponse(
        {
            "schema_version": CLAIMS_SCHEMA_VERSION,
            "generated_at": to_utc_iso(utcnow()),
            "claims": claims,
        },
        headers={"Cache-Control": "no-store"},
    )
