"""Read-only workspace operation history for entitled owners/admins (#2739)."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from config.plan_tiers import has_feature
from config.retention import operation_history_retention_days
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import Workspace
from services.operation_history import read_history
from services.operation_history_writer import OPERATIONS
from services.permission_service import PermissionService
from utils.exceptions import FeatureNotAvailableError, ValidationError

router = APIRouter(tags=["workspaces"])


class OperationHistoryItem(TZAwareBaseModel):
    id: UUID
    occurred_at: datetime
    context_id: UUID | None
    actor_user_id: str | None
    actor_name: str | None
    subject_user_id: str | None
    agent_id: UUID | None
    principal_kind: str
    credential_id: str | None
    client_id: str | None
    surface: str
    operation: str
    target_type: str
    target_id: str | None
    outcome: str
    count: int
    changed_fields: list[str]


class OperationHistoryPage(TZAwareBaseModel):
    items: list[OperationHistoryItem]
    total: int
    total_capped: bool = False
    next_cursor: str | None
    # Effective retention of this workspace: older events are not returned.
    retention_days: int


@router.get("/workspaces/{workspace_id}/operation-history", response_model=OperationHistoryPage)
async def get_operation_history(
    workspace_id: UUID,
    user: SessionUser,
    response: Response,
    db: AsyncSession = Depends(get_db),
    query: str = Query("", max_length=120),
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    context_id: UUID | None = None,
    actor: str | None = Query(None, max_length=255),
    agent_id: UUID | None = None,
    operation: str | None = Query(None, max_length=40),
    target_type: str | None = Query(
        None, pattern="^(memory|context|member|credential|integration|sleep)$"
    ),
    outcome: str | None = Query(None, pattern="^(success|failure|denied)$"),
    since: datetime | None = None,
    until: datetime | None = None,
) -> OperationHistoryPage:
    """Search safe identifiers/actor names/field names, never memory or credential contents.

    Authorization and the selected workspace's effective entitlement are checked
    on every request, including pagination and requests after a downgrade. Only
    events inside the workspace's retention (90 days plus its add-on) are
    returned; ``retention_days`` reports that window.
    """
    response.headers["Cache-Control"] = "no-store"
    await PermissionService(db).check_workspace_admin(user["user_id"], workspace_id)
    row = (
        await db.execute(
            select(Workspace.plan_name, Workspace.addon_history_retention_days_bonus).where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None or not has_feature(row.plan_name, "operation_history"):
        plan = row.plan_name if row is not None else None
        raise FeatureNotAvailableError.for_feature(plan or "free", "operation_history")
    retention_days = operation_history_retention_days(row.addon_history_retention_days_bonus)
    if operation is not None and operation not in OPERATIONS:
        raise ValidationError("Invalid operation", field="operation")
    for field, value in (("since", since), ("until", until)):
        if value is not None and value.tzinfo is None:
            raise ValidationError("Time filters require a timezone", field=field)
    if since and until and since > until:
        raise ValidationError("Start must not be after end", field="since")
    return OperationHistoryPage(
        retention_days=retention_days,
        **await read_history(
            db,
            workspace_id=workspace_id,
            user_id=user["user_id"],
            limit=limit,
            retention_days=retention_days,
            query=query,
            cursor=cursor,
            context_id=context_id,
            actor=actor,
            agent_id=agent_id,
            operation=operation,
            target_type=target_type,
            outcome=outcome,
            since=since,
            until=until,
        ),
    )
