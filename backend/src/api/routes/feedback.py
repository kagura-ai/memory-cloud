"""REST route for the retrieval feedback signal (Issue #888, epic #885).

``POST /api/v1/contexts/{context_id}/feedback`` records an append-only "was this
recalled memory useful for this query" event. Feedback lives in its own table and
is never embedded, so it cannot pollute ``recall()``.

Access mirrors the established context-scoped pattern (#906): recording feedback
is a **read-adjacent** action — anyone who can read the context (VIEWER) consumes
recall and may rate it — so it gates on ``check_context_access(VIEWER)``, not
write. The caller's ``user_id`` is stored for attribution.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser
from auth.workspace_roles import ContextRole
from db.base import get_db
from models.retrieval_feedback import NOTE_MAX_LEN, QUERY_MAX_LEN
from services.feedback_service import FeedbackService
from services.permission_service import PermissionService
from utils.exceptions import MemoryCloudException
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/contexts", tags=["retrieval-feedback"])


class FeedbackRequest(BaseModel):
    """Body for recording retrieval feedback."""

    memory_id: UUID = Field(..., description="The recalled memory being rated")
    helpful: bool = Field(..., description="Was this memory useful for the query?")
    query: str | None = Field(
        None,
        max_length=QUERY_MAX_LEN,
        description="The recall query this feedback is about (optional)",
    )
    note: str | None = Field(
        None,
        max_length=NOTE_MAX_LEN,
        description="Optional free-text note (e.g. why it was wrong). Max 2000 chars.",
    )


class FeedbackResponse(BaseModel):
    feedback_id: UUID
    memory_id: UUID
    helpful: bool


async def get_feedback_service(db: AsyncSession = Depends(get_db)) -> FeedbackService:
    return FeedbackService(db)


async def get_permission_service(db: AsyncSession = Depends(get_db)) -> PermissionService:
    return PermissionService(db)


@router.post(
    "/{context_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_feedback(
    context_id: UUID,
    body: FeedbackRequest,
    user: APIKeyOrSessionUser,
    service: FeedbackService = Depends(get_feedback_service),
    perm: PermissionService = Depends(get_permission_service),
):
    """Record an append-only retrieval-feedback event for a recalled memory."""
    user_id = user.get("user_id")
    logger.info(
        "retrieval_feedback_requested",
        user_id=user_id,
        context_id=str(context_id),
        memory_id=str(body.memory_id),
        helpful=body.helpful,
    )
    try:
        # Read-adjacent: VIEWER may rate recall. Uniform 404 on unreachable
        # context (IDOR guard), same posture as the other context-scoped routes.
        await perm.check_context_access(
            user_id, context_id, required_role=ContextRole.VIEWER, operation="feedback"
        )
        row = await service.record_feedback(
            context_id=context_id,
            memory_id=body.memory_id,
            helpful=body.helpful,
            user_id=user_id,
            query=body.query,
            note=body.note,
        )
        return FeedbackResponse(feedback_id=row.id, memory_id=row.memory_id, helpful=row.helpful)
    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error(
            "retrieval_feedback_failed",
            user_id=user_id,
            context_id=str(context_id),
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to record retrieval feedback",
        ) from e
