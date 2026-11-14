"""Scoped service metadata admission; content/run APIs are delivered separately."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_bearer import sync_service_token
from db.base import get_db
from services.sync_credentials import SyncCredentialError, sync_token_boundary
from utils.exceptions import MemoryCloudException
from utils.logger import get_logger

logger = get_logger(__name__)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class SyncWorkerError(BaseModel):
    """Fixed protocol error without reflected credential or tenant details."""

    error: str


class SyncWorkerRoute(APIRoute):
    """No-store for every outcome, including dependency and database errors."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
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
            except SyncCredentialError as exc:
                response = JSONResponse({"error": exc.error}, status_code=exc.status)
            except MemoryCloudException as exc:
                response = JSONResponse({"error": exc.error_code}, status_code=exc.status_code)
            except Exception as exc:
                logger.warning("sync_service_unavailable", failure_type=type(exc).__name__)
                response = JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
            response.headers.update(NO_STORE)
            return response

        return handler


class SyncWorkerInstallationView(BaseModel):
    """Operational target identity only; no product/FAQ content or new authority."""

    installation_id: UUID
    workspace_id: UUID
    service_id: str
    resource_pk: UUID
    context_id: UUID
    endpoint_id: UUID
    resource_id: str
    generation: int
    operations: list[str]


router = APIRouter(
    prefix="/sync/installations",
    tags=["workspace-sync"],
    route_class=SyncWorkerRoute,
    responses={
        status: {"model": SyncWorkerError} for status in (400, 401, 403, 404, 429, 503, "default")
    },
)


@router.get("/{installation_id}", response_model=SyncWorkerInstallationView)
async def inspect_sync_target(
    installation_id: UUID,
    token: Annotated[str, Depends(sync_service_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SyncWorkerInstallationView:
    """Inspect this installation only under current service and paid authority."""
    async with sync_token_boundary(
        db, token, installation_id, operation="results:read"
    ) as authority:
        row = authority.installation
        result = SyncWorkerInstallationView(
            installation_id=row.id,
            workspace_id=row.workspace_id,
            service_id=row.service_id,
            resource_pk=row.resource_pk,
            context_id=row.context_id,
            endpoint_id=row.endpoint_id,
            resource_id=row.resource_id,
            generation=row.generation,
            operations=list(row.operations),
        )
    return result
