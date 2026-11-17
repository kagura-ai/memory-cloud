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
from models.sync_setup_handoff_schemas import SyncSetupHandoffCreate, SyncSetupHandoffView
from services.sync_installations import (
    activate_installation,
    create_authorization,
    inspect_installation,
    revoke_authorization,
    unlink_installation,
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
                response = JSONResponse({"error": exc.error_code}, status_code=exc.status_code)
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
