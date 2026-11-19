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
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.single_authorization import sole_authorization
from config.settings import get_settings
from db.base import get_db
from mcp_server.public.domains import claimable_hostname, public_domains_enabled
from mcp_server.public.publication import (
    load_serving_endpoint_for_alias,
    public_endpoints_enabled,
)
from models.public_mcp_domain import PublicMcpDomain
from services.domain_tls_observations import DomainTLSReport
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal-public-mcp-domains"])

PERMISSION_PATH = "/public-mcp-domains/tls-permission"
PERMISSION_CONCURRENCY = 4
PERMISSION_TIMEOUT_SECONDS: float = 3

# Taken only when a slot is free at once (``locked()`` first), so a call never
# waits for one and the semaphore never binds to an event loop.
_SLOTS = asyncio.Semaphore(PERMISSION_CONCURRENCY)


@router.put("/public-mcp-domains/tls-observation", include_in_schema=False)
async def receive_tls_observation(
    request: Request, report: DomainTLSReport, db: Annotated[AsyncSession, Depends(get_db)]
) -> Response:
    """Accept the dedicated reporter's latest observation; never authorize serving."""
    token = get_settings().mcp_public_domains_tls_reporter_token
    if not token or not public_domains_enabled() or not public_endpoints_enabled():
        return Response(status_code=404)
    authorization = sole_authorization(request) or ""
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        return Response(status_code=401)
    row = await db.scalar(
        select(PublicMcpDomain).where(PublicMcpDomain.id == report.domain_id).with_for_update()
    )
    if row is None or not report.matches(row):
        return Response(status_code=404)
    now = utcnow().replace(tzinfo=UTC)
    if not report.tls.observed_at <= now < report.tls.valid_until:
        return Response(status_code=409)
    if row.tls_report is not None:
        try:
            previous = DomainTLSReport.model_validate(row.tls_report)
        except ValidationError:
            previous = None
        if previous is not None and previous.matches(row):
            if previous == report:
                return Response(status_code=204)
            if previous.tls.observed_at >= report.tls.observed_at:
                return Response(status_code=409)
    row.tls_report = report.model_dump(mode="json")
    await db.commit()
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
