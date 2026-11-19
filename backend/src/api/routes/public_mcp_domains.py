"""Owner routes of verified custom domains of a public MCP endpoint (#2809).

``GET/POST /api/v1/resources/{resource_id}/public-mcp/domains``,
``POST .../domains/{domain_id}/verify`` and ``DELETE .../domains/{domain_id}``.
A custom domain is an alias of the resource's live publication, served at
``https://<hostname>/mcp`` once the owner proved control of the name with a
DNS TXT record (``docs/public-mcp-domains.md``).

Every route answers 404 before any statement while
``MCP_PUBLIC_DOMAINS_ENABLED`` is off, and ``Cache-Control: no-store`` on
every answer except the generic request-validation 422 (a body, path or
query that fails validation), which the global handler sends without it.
The workspace comes from the authenticated principal and the
publication from ``(workspace, resource slug)``; adding and verifying a
domain need the workspace's canonical owner, reading and removing one any
workspace owner. The states, limits and errors live in
``services.public_mcp_domains``.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.public_mcp_endpoints import ResourceSlug
from auth.dependencies import WorkspaceOwner
from config.settings import get_settings
from db.base import get_db
from mcp_server.public.domains import challenge_record, public_domains_enabled
from mcp_server.public.publication import load_serving_endpoint_for_alias
from models.api_base import TZAwareBaseModel
from models.public_mcp_domain import PublicMcpDomain
from services.public_mcp_domains import (
    attach_domain,
    detach_domain,
    list_domains,
    verify_domain,
)
from utils.exceptions import MemoryCloudException, NotFoundException

NO_STORE = "no-store"

DomainStateField = Literal["pending", "active", "suspended"]
SuspendedReasonField = Literal["proof_lost", "right_ended", "reserved"]
RoutingStatusField = Literal["ok", "mismatch", "unresolved"]
ProofResultField = Literal["ok", "missing", "unavailable"]


class _NoStoreRoute(APIRoute):
    """``Cache-Control: no-store`` on every answer, refusals included.

    The exception is the generic request-validation 422 (a body, path or
    query that fails validation): it is raised before the endpoint runs, this
    wrapper passes it through unchanged, and the global handler sends it
    without the header.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the handler: success and typed refusals carry the header."""
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except MemoryCloudException as exc:
                # The global handler sends ``response_headers`` (#2679).
                headers = dict(getattr(exc, "response_headers", None) or {})
                headers["Cache-Control"] = NO_STORE
                exc.__dict__["response_headers"] = headers
                raise
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": NO_STORE}
                raise
            response.headers["Cache-Control"] = NO_STORE
            return response

        return handler


async def public_mcp_domains_route_enabled() -> None:
    """404 before any dependency or statement while the switch is off."""
    if not public_domains_enabled():
        raise NotFoundException("Public MCP domain")


router = APIRouter(
    prefix="/resources",
    tags=["resources"],
    route_class=_NoStoreRoute,
    dependencies=[Depends(public_mcp_domains_route_enabled)],
)

DomainId = Path(..., description="The custom domain's `domain_id`.")


class PublicMcpDomainCreate(BaseModel):
    """A host name to add as a custom domain of the publication."""

    hostname: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="ASCII host name (A-labels for international names), no port, no path",
    )


class PublicMcpDomainVerification(BaseModel):
    """The DNS record that proves control of the name."""

    record_type: Literal["TXT"] = "TXT"
    record_name: str = Field(..., description="Name of the TXT record")
    record_value: str = Field(..., description="Exact value of the TXT record")


class PublicMcpDomainRouting(BaseModel):
    """Where the name should point (informational: never a condition of serving)."""

    target: str = Field(..., description="MCP_PUBLIC_DOMAINS_ROUTING_TARGET (CNAME target)")
    status: RoutingStatusField | None = Field(
        None, description="Result of the last check; null before the first"
    )


class PublicMcpDomainLastCheck(TZAwareBaseModel):
    """The last completed DNS check."""

    result: ProofResultField | None = None
    checked_at: datetime | None = None


class PublicMcpDomainResponse(TZAwareBaseModel):
    """A custom domain of a public MCP endpoint."""

    domain_id: UUID
    hostname: str
    url: str = Field(..., description="The alias address: `https://<hostname>/mcp`")
    state: DomainStateField
    suspended_reason: SuspendedReasonField | None = None
    serving: bool = Field(..., description="Whether the alias answers right now")
    verification: PublicMcpDomainVerification
    routing: PublicMcpDomainRouting
    last_check: PublicMcpDomainLastCheck
    proof_ok_at: datetime | None = Field(None, description="Last time the proof was found")
    proof_missing_checks: int = Field(..., description="Confirmed absences since proof_ok_at")
    serve_until: datetime | None = Field(
        None, description="End of the lease; the alias is served only before it"
    )
    created_at: datetime
    verified_at: datetime | None = None


class PublicMcpDomainList(BaseModel):
    """The custom domains of a publication (released ones are not listed)."""

    domains: list[PublicMcpDomainResponse]


async def domain_response(db: AsyncSession, row: PublicMcpDomain) -> PublicMcpDomainResponse:
    serving = False
    if row.state == "active":
        endpoint = await load_serving_endpoint_for_alias(db, row.hostname)
        serving = endpoint is not None and endpoint.id == row.endpoint_id
    record_name, record_value = challenge_record(row.hostname, row.verification_token)
    return PublicMcpDomainResponse(
        domain_id=row.id,
        hostname=row.hostname,
        url=f"https://{row.hostname}/mcp",
        state=cast(DomainStateField, row.state),
        suspended_reason=cast(SuspendedReasonField | None, row.suspended_reason),
        serving=serving,
        verification=PublicMcpDomainVerification(
            record_name=record_name, record_value=record_value
        ),
        routing=PublicMcpDomainRouting(
            target=get_settings().mcp_public_domains_routing_target,
            status=cast(RoutingStatusField | None, row.routing_status),
        ),
        last_check=PublicMcpDomainLastCheck(
            result=cast(ProofResultField | None, row.last_proof_result),
            checked_at=row.last_checked_at,
        ),
        proof_ok_at=row.proof_ok_at,
        proof_missing_checks=row.proof_missing_checks,
        serve_until=row.serve_until,
        created_at=row.created_at,
        verified_at=row.verified_at,
    )


@router.get("/{resource_id}/public-mcp/domains", response_model=PublicMcpDomainList)
async def list_public_mcp_domains(
    owner: WorkspaceOwner,
    resource_id: str = ResourceSlug,
    db: AsyncSession = Depends(get_db),
) -> PublicMcpDomainList:
    """List the custom domains of the resource's live public MCP endpoint."""
    _, workspace_id = owner
    rows = await list_domains(db, workspace_id, resource_id)
    return PublicMcpDomainList(domains=[await domain_response(db, row) for row in rows])


@router.post(
    "/{resource_id}/public-mcp/domains",
    response_model=PublicMcpDomainResponse,
    status_code=201,
)
async def add_public_mcp_domain(
    body: PublicMcpDomainCreate,
    owner: WorkspaceOwner,
    resource_id: str = ResourceSlug,
    db: AsyncSession = Depends(get_db),
) -> PublicMcpDomainResponse:
    """Add a host name as a custom domain (``pending``) and return its TXT record.

    The canonical owner only. The domain serves nothing until it is verified.
    """
    user_id, workspace_id = owner
    row = await attach_domain(db, user_id, workspace_id, resource_id, body.hostname)
    return await domain_response(db, row)


@router.post(
    "/{resource_id}/public-mcp/domains/{domain_id}/verify",
    response_model=PublicMcpDomainResponse,
)
async def verify_public_mcp_domain(
    owner: WorkspaceOwner,
    resource_id: str = ResourceSlug,
    domain_id: UUID = DomainId,
    db: AsyncSession = Depends(get_db),
) -> PublicMcpDomainResponse:
    """Check the TXT record now; a found proof activates a ``pending`` domain.

    The canonical owner only. Limited per workspace (verify calls per hour,
    activation attempts per day).
    """
    user_id, workspace_id = owner
    row = await verify_domain(db, user_id, workspace_id, resource_id, domain_id)
    return await domain_response(db, row)


@router.delete("/{resource_id}/public-mcp/domains/{domain_id}", status_code=204)
async def remove_public_mcp_domain(
    owner: WorkspaceOwner,
    resource_id: str = ResourceSlug,
    domain_id: UUID = DomainId,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a custom domain; the alias answers 404 from the next request on."""
    user_id, workspace_id = owner
    await detach_domain(db, user_id, workspace_id, resource_id, domain_id)
    return Response(status_code=204)
