"""System-admin lifecycle API for worker app identities (#1315).

Audit discipline (#1339): every lifecycle mutation (create / update /
rotate-secret) emits a structlog success event AFTER commit with the actor
and non-secret fields only, and the unexpected-exception arms log the
underlying cause with ``exc_info=True`` before collapsing it into
``WorkerAppOperationError`` (whose fixed message the generic handler logs
without the chained cause). Secret material and ciphertext must never
appear in any log call — pinned by tests/api/test_worker_apps.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Path, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_admin
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.worker_app import WorkerAppIdentity
from services.worker_app_identity import (
    MAX_RETIRING_WINDOW_SECONDS,
    WorkerAppIdentityService,
    identity_revision,
)
from utils.datetime import to_utc_iso
from utils.exceptions import ConflictError, MemoryCloudException, WorkerAppOperationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/worker-apps", tags=["admin-worker-apps"])

Platform = Literal["slack", "discord", "teams"]
AppStatus = Literal["active", "disabled"]


class WorkerAppAdminResponse(TZAwareBaseModel):
    platform: str
    app_key: str
    display_name: str
    status: str
    revision: str
    has_active_secret: bool
    active_secret_revision: int | None
    retiring_secret_revision: int | None
    retiring_valid_until: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkerAppCreateRequest(BaseModel):
    platform: Platform
    app_key: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    display_name: str = Field(..., min_length=1, max_length=255)
    signing_secret: str = Field(..., min_length=1, max_length=512)


class WorkerAppUpdateRequest(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=255)
    status: AppStatus | None = None

    @model_validator(mode="after")
    def require_change(self) -> WorkerAppUpdateRequest:
        if self.display_name is None and self.status is None:
            raise ValueError("At least one field must be provided")
        return self


class WorkerAppRotateSecretRequest(BaseModel):
    signing_secret: str = Field(..., min_length=1, max_length=512)
    retiring_for_seconds: int = Field(3600, ge=0, le=MAX_RETIRING_WINDOW_SECONDS)


def _admin_response(identity: WorkerAppIdentity) -> WorkerAppAdminResponse:
    """Serialize lifecycle metadata while keeping both secret columns opaque."""
    return WorkerAppAdminResponse(
        platform=identity.platform,
        app_key=identity.app_key,
        display_name=identity.display_name,
        status=identity.status,
        revision=identity_revision(identity),
        has_active_secret=bool(identity.active_signing_secret_encrypted),
        active_secret_revision=identity.active_secret_revision,
        retiring_secret_revision=identity.retiring_secret_revision,
        retiring_valid_until=identity.retiring_valid_until,
        created_at=identity.created_at,
        updated_at=identity.updated_at,
    )


@router.get("", response_model=list[WorkerAppAdminResponse])
async def list_worker_apps(
    _admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[WorkerAppAdminResponse]:
    identities = await WorkerAppIdentityService(db).list_identities()
    return [_admin_response(identity) for identity in identities]


@router.post("", response_model=WorkerAppAdminResponse, status_code=status.HTTP_201_CREATED)
async def create_worker_app(
    request: WorkerAppCreateRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> WorkerAppAdminResponse:
    try:
        identity = await WorkerAppIdentityService(db).create_identity(
            platform=request.platform,
            app_key=request.app_key,
            display_name=request.display_name,
            signing_secret=request.signing_secret,
            actor_id=admin["user_id"],
        )
        await db.commit()
        await db.refresh(identity)
    except MemoryCloudException:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Worker app identity already exists") from exc
    except Exception as exc:
        await db.rollback()
        # #1339: the generic exception handler logs only the fixed
        # WorkerAppOperationError message — capture the real cause here or a
        # failed lifecycle write is undiagnosable. Tracebacks carry no
        # request body, so no secret material reaches the log.
        logger.error(
            "worker_app_operation_failed",
            operation="create",
            actor=admin["user_id"],
            platform=request.platform,
            app_key=request.app_key,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise WorkerAppOperationError("create") from exc
    # #1339: post-commit audit event — non-secret fields only.
    logger.info(
        "worker_app_created",
        actor=admin["user_id"],
        platform=identity.platform,
        app_key=identity.app_key,
        status=identity.status,
        active_secret_revision=identity.active_secret_revision,
    )
    return _admin_response(identity)


@router.patch("/{platform}/{app_key}", response_model=WorkerAppAdminResponse)
async def update_worker_app(
    request: WorkerAppUpdateRequest,
    platform: Platform,
    app_key: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> WorkerAppAdminResponse:
    try:
        identity = await WorkerAppIdentityService(db).update_identity(
            platform=platform,
            app_key=app_key,
            display_name=request.display_name,
            status=request.status,
            actor_id=admin["user_id"],
        )
        await db.commit()
        await db.refresh(identity)
    except MemoryCloudException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.error(
            "worker_app_operation_failed",
            operation="update",
            actor=admin["user_id"],
            platform=platform,
            app_key=app_key,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise WorkerAppOperationError("update") from exc
    logger.info(
        "worker_app_updated",
        actor=admin["user_id"],
        platform=identity.platform,
        app_key=identity.app_key,
        status=identity.status,
        # "provided", not "changed" — the route never sees the old value.
        display_name_provided=request.display_name is not None,
        # Revision context so an enable/disable can be correlated with the
        # secret material that was active/retiring at that moment. Serialized
        # to a string — a raw datetime would break the JSON log renderer.
        active_secret_revision=identity.active_secret_revision,
        retiring_secret_revision=identity.retiring_secret_revision,
        retiring_valid_until=to_utc_iso(identity.retiring_valid_until),
    )
    return _admin_response(identity)


@router.post("/{platform}/{app_key}/rotate-secret", response_model=WorkerAppAdminResponse)
async def rotate_worker_app_secret(
    request: WorkerAppRotateSecretRequest,
    platform: Platform,
    app_key: str = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> WorkerAppAdminResponse:
    try:
        identity = await WorkerAppIdentityService(db).rotate_secret(
            platform=platform,
            app_key=app_key,
            signing_secret=request.signing_secret,
            retiring_for_seconds=request.retiring_for_seconds,
            actor_id=admin["user_id"],
        )
        await db.commit()
        await db.refresh(identity)
    except MemoryCloudException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.error(
            "worker_app_operation_failed",
            operation="rotate",
            actor=admin["user_id"],
            platform=platform,
            app_key=app_key,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise WorkerAppOperationError("rotate") from exc
    logger.info(
        "worker_app_secret_rotated",
        actor=admin["user_id"],
        platform=identity.platform,
        app_key=identity.app_key,
        status=identity.status,
        active_secret_revision=identity.active_secret_revision,
        retiring_secret_revision=identity.retiring_secret_revision,
        retiring_for_seconds=request.retiring_for_seconds,
    )
    return _admin_response(identity)
