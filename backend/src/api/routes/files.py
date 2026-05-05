"""REST routes for platform-managed file storage (Issue #485).

Thin handlers that delegate to ``FileStorageService``. Exceptions
(``NotFoundException`` / ``ValidationError`` / ``ConflictError`` /
``QuotaExceededError``) are mapped to HTTP responses by the global
``MemoryCloudException`` handler in ``api.main``.

Auth: ``APIKeyOrSessionUser`` for all endpoints — the same dual-mode
auth used by the rest of the workspace surface. ``workspace_id`` comes
from the request body or query (and is verified by the service).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from services.file_storage_service import FileStorageService
from services.permission_service import PermissionService
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

# Phase 1 100 MiB cap (Field constraint mirrors settings.file_object_max_size_mb;
# the service re-validates against the runtime setting so an env-var override
# does not get bypassed by stale schema validation).
_MAX_PHASE1_SIZE_BYTES = 100 * 1024 * 1024


class FileReserveRequest(BaseModel):
    """Body for ``POST /api/v1/files/reserve``."""

    workspace_id: UUID
    filename: str = Field(min_length=1, max_length=512)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=_MAX_PHASE1_SIZE_BYTES)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Lower-case hex sha256 of the bytes the client will PUT",
    )


class FileReserveResponse(TZAwareBaseModel):
    file_id: UUID
    upload_url: str
    expires_at: datetime


class FileConfirmRequest(BaseModel):
    """Body for ``POST /api/v1/files/{file_id}/confirm``."""

    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )


class FileObjectOut(TZAwareBaseModel):
    id: UUID
    workspace_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    created_at: datetime
    uploaded_at: datetime | None = None


class FileDownloadUrlOut(TZAwareBaseModel):
    download_url: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_out(file) -> FileObjectOut:
    """Convert a ``FileObject`` ORM row to the API response model."""
    return FileObjectOut(
        id=file.id,
        workspace_id=file.workspace_id,
        filename=file.filename,
        content_type=file.content_type,
        size_bytes=file.size_bytes,
        sha256=file.sha256,
        status=file.status,
        created_at=file.created_at,
        uploaded_at=file.uploaded_at,
    )


# ---------------------------------------------------------------------------
# Auth helper — workspace boundary enforcement
# ---------------------------------------------------------------------------


async def _enforce_workspace_membership(
    db: AsyncSession,
    user: dict,
    workspace_id: UUID,
) -> None:
    """Authorize ``user`` for ``workspace_id`` (member or higher).

    ``APIKeyOrSessionUser`` proves identity, not workspace membership.
    Without this gate an authenticated user from workspace A could
    pass workspace B's id in the request body / query and access /
    enumerate B's files. Mirrors the pattern at
    ``api/routes/resource_ingest.py``.
    """
    permissions = PermissionService(db)
    await permissions.check_workspace_access(
        user_id=str(user.get("user_id", "")),
        workspace_id=workspace_id,
        required_role="member",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/reserve", response_model=FileReserveResponse, status_code=201)
async def reserve_upload(
    body: FileReserveRequest,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> FileReserveResponse:
    """Reserve quota and return a presigned PUT URL for the upload.

    The client computes the sha256 ahead of time so the server can dedup
    against the active set on this workspace. Repeated calls with the
    same sha256 from the same workspace return 409 with the existing
    ``file_id`` (idempotent — the SDK can use the existing one).
    """
    await _enforce_workspace_membership(db, user, body.workspace_id)
    service = FileStorageService(db)
    result = await service.reserve_upload(
        workspace_id=body.workspace_id,
        created_by=str(user.get("user_id", "")),
        filename=body.filename,
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        sha256=body.sha256.lower(),
    )
    return FileReserveResponse(
        file_id=result.file_id,
        upload_url=result.upload_url,
        expires_at=result.expires_at,
    )


@router.post("/{file_id}/confirm", response_model=FileObjectOut)
async def confirm_upload(
    file_id: UUID,
    body: FileConfirmRequest,
    user: APIKeyOrSessionUser,
    workspace_id: UUID = Query(..., description="Owning workspace"),
    db: AsyncSession = Depends(get_db),
) -> FileObjectOut:
    """Verify the upload landed in R2 and finalize the row.

    Idempotent: confirming an already-uploaded file with a matching
    sha256 returns the existing row unchanged.
    """
    await _enforce_workspace_membership(db, user, workspace_id)
    service = FileStorageService(db)
    file = await service.confirm_upload(
        workspace_id=workspace_id,
        file_id=file_id,
        sha256=body.sha256.lower(),
    )
    return _to_out(file)


@router.get("/{file_id}/download-url", response_model=FileDownloadUrlOut)
async def get_download_url(
    file_id: UUID,
    user: APIKeyOrSessionUser,
    workspace_id: UUID = Query(..., description="Owning workspace"),
    db: AsyncSession = Depends(get_db),
) -> FileDownloadUrlOut:
    """Return a short-lived presigned GET URL."""
    await _enforce_workspace_membership(db, user, workspace_id)
    service = FileStorageService(db)
    url = await service.get_presigned_download(
        workspace_id=workspace_id,
        file_id=file_id,
    )
    return FileDownloadUrlOut(download_url=url)


@router.delete("/{file_id}", status_code=204, response_model=None)
async def delete_file(
    file_id: UUID,
    user: APIKeyOrSessionUser,
    workspace_id: UUID = Query(..., description="Owning workspace"),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete the file and immediately release the quota.

    The R2 binary lingers for 7 days (sweeper handles deletion); the
    workspace counter is decremented in the same transaction (R5).
    """
    await _enforce_workspace_membership(db, user, workspace_id)
    service = FileStorageService(db)
    await service.delete_file(workspace_id=workspace_id, file_id=file_id)


@router.get("", response_model=list[FileObjectOut])
async def list_files(
    user: APIKeyOrSessionUser,
    workspace_id: UUID = Query(..., description="Owning workspace"),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[FileObjectOut]:
    """List uploaded, non-deleted files in the workspace, newest first."""
    await _enforce_workspace_membership(db, user, workspace_id)
    service = FileStorageService(db)
    files = await service.list_files(workspace_id=workspace_id, limit=limit)
    return [_to_out(f) for f in files]
