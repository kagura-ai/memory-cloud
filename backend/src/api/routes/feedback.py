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

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from auth.agent_scope import get_agent_scope
from auth.dependencies import APIKeyOrSessionUser, require_workspace_admin
from auth.workspace_roles import ContextRole
from db.base import get_db
from models.retrieval_feedback import NOTE_MAX_LEN, QUERY_MAX_LEN
from services.feedback_service import (
    HOST_EXPERIMENT_ID_MAX_LEN,
    HOST_VERDICT_REFERENCE_MAX_LEN,
    FeedbackService,
)
from services.permission_service import PermissionService
from utils.exceptions import (
    AuthorizationError,
    InternalError,
    MemoryCloudException,
    NotFoundException,
)
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


class HostFeedbackRequest(BaseModel):
    """Trusted operator verdict; provenance is intentionally not caller-settable."""

    memory_id: UUID = Field(..., description="The recalled memory being rated")
    helpful: bool = Field(..., description="Independent host verdict")
    query: str | None = Field(None, max_length=QUERY_MAX_LEN)
    verdict_source: Literal["objective_check", "trusted_host_check", "hitl_approval"]
    verdict_reference: str = Field(..., max_length=HOST_VERDICT_REFERENCE_MAX_LEN)
    experiment_id: str | None = Field(None, max_length=HOST_EXPERIMENT_ID_MAX_LEN)
    note: str | None = Field(None, max_length=NOTE_MAX_LEN)

    @field_validator("verdict_reference")
    @classmethod
    def validate_verdict_reference(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("verdict_reference must not be blank")
        return value

    @field_validator("experiment_id")
    @classmethod
    def validate_experiment_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("experiment_id must not be blank")
        return value


async def get_feedback_service(db: AsyncSession = Depends(get_db)) -> FeedbackService:
    return FeedbackService(db)


async def get_permission_service(db: AsyncSession = Depends(get_db)) -> PermissionService:
    return PermissionService(db)


async def require_host_feedback_operator(
    user: dict = Depends(require_workspace_admin),
) -> dict:
    """Allow trusted owner/admin principals, but never an agent-bound key."""
    scope = get_agent_scope()
    if scope is not None:
        logger.warning(
            "host_feedback_agent_credential_rejected",
            user_id=user.get("user_id"),
            agent_id=str(scope.agent_id),
        )
        raise AuthorizationError(reason="agent_bound_credential")
    return user


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


@router.post(
    "/{context_id}/host-feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_host_feedback(
    context_id: UUID,
    body: HostFeedbackRequest,
    user: dict = Depends(require_host_feedback_operator),
    service: FeedbackService = Depends(get_feedback_service),
    perm: PermissionService = Depends(get_permission_service),
):
    """Record a server-stamped host verdict from a trusted operator."""
    user_id = user["user_id"]
    active_workspace_id = UUID(str(user["current_workspace_id"]))
    try:
        context = await perm.resolve_context_for_workspace_read(
            user_id,
            context_id,
            required_role="admin",
            key_workspace_id=user.get("api_key_workspace_id"),
        )
        # Session/global-key operators are membership-authorized across their
        # workspaces; this mutation is deliberately confined to the selected one.
        if context.workspace_id != active_workspace_id:
            raise NotFoundException("Context")

        # Three credential shapes reach this operator endpoint (all built in
        # auth.dependencies): API keys carry api_key_workspace_id, OAuth
        # Bearer principals carry oauth_scope (and are NOT workspace-bound),
        # session cookies carry neither. Discriminate on those markers so the
        # audit attribution names the real credential type.
        if "api_key_workspace_id" in user:
            via = "api_key"
        elif "oauth_scope" in user:
            via = "oauth_bearer"
        else:
            via = "session"
        actor_metadata = {"via": via}
        if user.get("api_key_prefix"):
            actor_metadata["key_prefix"] = user["api_key_prefix"]

        row = await service.record_host_feedback(
            context_id=context_id,
            memory_id=body.memory_id,
            helpful=body.helpful,
            user_id=user_id,
            actor_email=user.get("email"),
            actor_metadata=actor_metadata,
            query=body.query,
            verdict_source=body.verdict_source,
            verdict_reference=body.verdict_reference,
            experiment_id=body.experiment_id,
            note=body.note,
        )
        return FeedbackResponse(feedback_id=row.id, memory_id=row.memory_id, helpful=row.helpful)
    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error(
            "host_feedback_failed",
            user_id=user_id,
            context_id=str(context_id),
            memory_id=str(body.memory_id),
            error=str(e),
        )
        raise InternalError("Failed to record host feedback") from e
