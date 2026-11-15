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

from fastapi import APIRouter, Request, Response

from db.base import get_db
from mcp_server.public.domains import claimable_hostname, public_domains_enabled
from mcp_server.public.publication import (
    load_serving_endpoint_for_alias,
    public_endpoints_enabled,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal-public-mcp-domains"])

PERMISSION_PATH = "/public-mcp-domains/tls-permission"
PERMISSION_CONCURRENCY = 4
PERMISSION_TIMEOUT_SECONDS: float = 3

# Taken only when a slot is free at once (``locked()`` first), so a call never
# waits for one and the semaphore never binds to an event loop.
_SLOTS = asyncio.Semaphore(PERMISSION_CONCURRENCY)


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
