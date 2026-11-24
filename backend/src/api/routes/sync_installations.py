"""Scoped owner controls and current-member inspection of synchronization targets."""

import asyncio
import re
from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.sync_feeds import bounded_body, exact_query, json_body
from api.routes.sync_publication_domains import (
    PublicationDomainObservation,
    observe_publication_domain,
)
from auth.oidc_bearer import identity_access_token
from auth.oidc_server import credential_hash
from auth.sync_identity import SyncPrincipal, load_sync_identity, sync_manager, sync_reader
from config.settings import get_settings
from db.base import get_db
from models.sync_installation_schemas import (
    SyncActivation,
    SyncAuthorizationCreate,
    SyncAuthorizationView,
    SyncInstallationView,
)
from models.sync_publication_domain_schemas import (
    PublicationDomainClaim,
    PublicationDomainCommand,
    PublicationDomainCommandResult,
)
from models.sync_publication_text_schemas import PublicationTextResult, PublicationTextUpdate
from models.sync_setup_handoff_schemas import SyncSetupHandoffCreate, SyncSetupHandoffView
from services.public_mcp_text import update_installation_public_text
from services.sync_contract import (
    DOMAIN_OPERATIONS_CAPABILITY,
    TEXT_OPERATIONS_CAPABILITY,
    sync_configuration,
)
from services.sync_installations import (
    activate_installation,
    create_authorization,
    inspect_installation,
    revoke_authorization,
    unlink_installation,
)
from services.sync_publication_domain_commands import (
    claim_publication_domain,
    remove_publication_domain,
    verify_publication_domain,
)
from services.sync_setup_handoff import create_setup_handoff
from utils.exceptions import (
    AuthorizationError,
    MemoryCloudException,
    NotFoundException,
    SyncProtocolError,
)
from utils.logger import get_logger

logger = get_logger(__name__)


class SyncControlError(BaseModel):
    """Fixed, non-reflective control-plane error response."""

    error: str


SYNC_RESPONSES = {
    status: {"model": SyncControlError} for status in (400, 401, 403, 404, 409, 429, 503, "default")
}


class SyncControlRoute(APIRoute):
    """Private, fixed errors across dependencies, validation and outages."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Apply no-store to every outcome, without reflecting request bodies or DB errors."""
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except HTTPException as exc:
                response = JSONResponse(
                    {"error": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )
            except RequestValidationError:
                response = JSONResponse({"error": "invalid_request"}, status_code=400)
            except (AuthorizationError, NotFoundException):
                response = JSONResponse({"error": "not_found"}, status_code=404)
            except MemoryCloudException as exc:
                response = JSONResponse(
                    {"error": exc.error_code},
                    status_code=exc.status_code,
                    headers=getattr(exc, "response_headers", None),
                )
            except Exception as exc:
                logger.warning("sync_control_unavailable", failure_type=type(exc).__name__)
                response = JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
            response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache"})
            return response

        from services.sync_audit import audited_http

        return audited_http(handler)


async def sync_control_enabled() -> None:
    """The new surface stays disabled until the operator completes registration and migration."""
    if not get_settings().sync_integrations_enabled:
        raise NotFoundException("Synchronization controls")


router = APIRouter(
    prefix="/workspaces/{workspace_id}",
    tags=["workspace-sync"],
    route_class=SyncControlRoute,
    dependencies=[Depends(sync_control_enabled)],
    responses=SYNC_RESPONSES,
)
Manager = Annotated[SyncPrincipal, Depends(sync_manager)]
Reader = Annotated[SyncPrincipal, Depends(sync_reader)]
Database = Annotated[AsyncSession, Depends(get_db)]
SETUP_BODY_TIMEOUT_SECONDS = 5.0
# #2995: 6,000 characters at the ``\uXXXX\uXXXX`` worst case of JSON escaping fit;
# a body within the character limits is never 413.
PUBLICATION_TEXT_MAX_BYTES = 98_304


@router.post(
    "/sync-billing-handoffs",
    response_model=SyncSetupHandoffView,
    openapi_extra=json_body(SyncSetupHandoffCreate),
)
async def setup_billing_handoff(
    workspace_id: UUID,
    request: Request,
    token: Annotated[str, Depends(identity_access_token)],
    db: Database,
) -> SyncSetupHandoffView:
    """Enter only add-on setup with explicit scoped, current canonical ownership."""
    exact_query(request)
    lengths = request.headers.getlist("content-length")
    if len(request.headers.getlist("content-type")) != 1 or len(lengths) > 1:
        raise SyncProtocolError("invalid_request")
    if lengths:
        if re.fullmatch(r"0|[1-9][0-9]{0,5}", lengths[0]) is None:
            raise SyncProtocolError("invalid_request")
        if int(lengths[0]) > 4096:
            raise SyncProtocolError("request_too_large", 413)
    try:
        body, _size = await asyncio.wait_for(
            bounded_body(request, SyncSetupHandoffCreate, 4096),
            timeout=SETUP_BODY_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise SyncProtocolError("invalid_request") from None
    # identity_access_token rejects other namespaces and checks rate before DB.
    # Read the bounded body before the first SQL, so a slow client holds no
    # connection/authority transaction. Recheck the scoped identity again inside
    # the service's nonblocking canonical-owner/registration transaction.
    principal = await load_sync_identity(db, credential_hash(token), "integrations:manage")
    return await create_setup_handoff(db, workspace_id, principal, body)


@router.post("/sync-authorizations", response_model=SyncAuthorizationView, status_code=201)
async def authorize_sync(
    workspace_id: UUID, body: SyncAuthorizationCreate, principal: Manager, db: Database
) -> SyncAuthorizationView:
    """Record bounded owner consent for the authenticated client's registered service."""
    return await create_authorization(db, workspace_id, principal, body)


@router.delete("/sync-authorizations/{authorization_id}", response_model=SyncAuthorizationView)
async def withdraw_sync(
    workspace_id: UUID, authorization_id: UUID, principal: Manager, db: Database
) -> SyncAuthorizationView:
    """Withdraw a pending approval, without changing an activated installation."""
    return await revoke_authorization(db, workspace_id, principal, authorization_id)


@router.post("/sync-installations", response_model=SyncInstallationView, status_code=201)
async def provision_sync(
    workspace_id: UUID, body: SyncActivation, principal: Manager, db: Database
) -> SyncInstallationView:
    """Create only the recorded, paid target; successful retries retain its identity."""
    return await activate_installation(db, workspace_id, body, principal)


@router.get("/sync-installations/{installation_id}", response_model=SyncInstallationView)
async def read_sync(
    workspace_id: UUID, installation_id: UUID, principal: Reader, db: Database
) -> SyncInstallationView:
    """Inspect a current member's visible target without treating metadata as a grant."""
    return await inspect_installation(db, workspace_id, principal, installation_id)


@router.delete("/sync-installations/{installation_id}", response_model=SyncInstallationView)
async def remove_sync(
    workspace_id: UUID, installation_id: UUID, principal: Manager, db: Database
) -> SyncInstallationView:
    """Revoke and advance generation, retaining the target and public endpoint."""
    return await unlink_installation(db, workspace_id, principal, installation_id)


@router.get(
    "/sync-installations/{installation_id}/publication-domains",
    response_model=PublicationDomainObservation,
)
async def read_publication_domain(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Reader, db: Database
) -> PublicationDomainObservation:
    """Observe only this owner's fixed publication target through scoped OIDC."""
    exact_query(request)
    if "public.domains.observation.v1" not in sync_configuration(get_settings()).capabilities:
        raise NotFoundException("Publication domain observation")
    return await observe_publication_domain(db, workspace_id, installation_id, principal)


async def publication_domain_operations_enabled() -> None:
    """#2921: the domain commands answer 404 before authentication while not advertised."""
    if DOMAIN_OPERATIONS_CAPABILITY not in sync_configuration(get_settings()).capabilities:
        raise NotFoundException("Publication domain operations")


DomainOperations = [Depends(publication_domain_operations_enabled)]


@router.post(
    "/sync-installations/{installation_id}/publication-domains/claim",
    response_model=PublicationDomainCommandResult,
    dependencies=DomainOperations,
    openapi_extra=json_body(PublicationDomainClaim),
)
async def claim_publication_domain_route(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Manager, db: Database
) -> PublicationDomainCommandResult:
    """Claim a custom domain for the installation's publication (experimental).

    The canonical owner with ``integrations:manage`` for the installation's
    browser client. The body carries the binding last read in the
    observation; the publication must have no open domain.
    """
    exact_query(request)
    body, _size = await bounded_body(request, PublicationDomainClaim)
    return await claim_publication_domain(db, workspace_id, installation_id, principal, body)


@router.post(
    "/sync-installations/{installation_id}/publication-domains/verify",
    response_model=PublicationDomainCommandResult,
    dependencies=DomainOperations,
    openapi_extra=json_body(PublicationDomainCommand),
)
async def verify_publication_domain_route(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Manager, db: Database
) -> PublicationDomainCommandResult:
    """Check the named custom domain's proof now and apply the result (experimental).

    The body carries the binding and the domain (id and state) last read in
    the observation; a domain no longer in that state is a stale command.
    """
    exact_query(request)
    body, _size = await bounded_body(request, PublicationDomainCommand)
    return await verify_publication_domain(db, workspace_id, installation_id, principal, body)


@router.post(
    "/sync-installations/{installation_id}/publication-domains/remove",
    response_model=PublicationDomainCommandResult,
    dependencies=DomainOperations,
    openapi_extra=json_body(PublicationDomainCommand),
)
async def remove_publication_domain_route(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Manager, db: Database
) -> PublicationDomainCommandResult:
    """Remove the named custom domain of the installation's publication (experimental).

    The body carries the binding and the domain (id and state) last read in
    the observation; a domain no longer in that state is a stale command.
    """
    exact_query(request)
    body, _size = await bounded_body(request, PublicationDomainCommand)
    return await remove_publication_domain(db, workspace_id, installation_id, principal, body)


async def publication_text_operations_enabled() -> None:
    """#2995: the public-text command answers 404 before authentication while not advertised."""
    if TEXT_OPERATIONS_CAPABILITY not in sync_configuration(get_settings()).capabilities:
        raise NotFoundException("Publication text operations")


@router.post(
    "/sync-installations/{installation_id}/publication-text",
    response_model=PublicationTextResult,
    dependencies=[Depends(publication_text_operations_enabled)],
    openapi_extra=json_body(PublicationTextUpdate),
)
async def set_publication_text_route(
    workspace_id: UUID,
    installation_id: UUID,
    request: Request,
    token: Annotated[str, Depends(identity_access_token)],
    db: Database,
) -> PublicationTextResult:
    """Set the public description and usage guide of the installation's endpoint (experimental).

    A managing application acting for the workspace's canonical owner, with
    ``integrations:manage`` for the installation's browser client. The body
    carries the binding last read in the installation's observation and the
    fields to set (omitted keeps, ``""`` or ``null`` clears); the public name
    is not accepted. The owner API's limits apply and the change is recorded
    as ``integration.updated`` with the acting client. An unlinked
    installation, another installation or workspace, or an endpoint that is
    no longer the binding's is the uniform 404.
    """
    exact_query(request)
    try:
        body, _size = await asyncio.wait_for(
            bounded_body(request, PublicationTextUpdate, PUBLICATION_TEXT_MAX_BYTES),
            timeout=SETUP_BODY_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise SyncProtocolError("invalid_request") from None
    # The bounded body is read before the first SQL, so a slow client holds no
    # connection; the scoped identity is checked again inside the admission.
    principal = await load_sync_identity(db, credential_hash(token), "integrations:manage")
    return await update_installation_public_text(db, workspace_id, installation_id, principal, body)
