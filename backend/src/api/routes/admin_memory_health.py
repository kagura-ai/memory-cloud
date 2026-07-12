"""Per-context memory-health report API (#1211 Phase 1, #1225 Phase 2).

One endpoint answering "is this partition's memory healthy?" from the
signals the system already emits (sleep reports, graph invariants, usage
stats, config posture) — thresholded ok/warn/fail per section so a
silent-judge-death class of failure (#1177) surfaces without an external
eval program.

Without ``context_id`` the endpoint returns the per-context breakdown (one
graded entry per owned context; the page-level overall is the worst entry).
With ``context_id=<uuid>`` it returns the 3-section detailed document for
that single context (ownership validated — un-owned or unknown → uniform
404). The sentinel ``context_id=unattributed`` targets signals recorded
without a context (account-wide sleep runs, cross-context recalls, legacy
rows). Self-scoped like the manual sleep trigger; workspace rollup is
Phase 3.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from db.base import get_db
from services.memory_health_service import MemoryHealthService
from utils.exceptions import MemoryCloudException
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/memory-health", tags=["admin-memory-health"])


HealthStatus = Literal["ok", "warn", "fail"]

# Query-param sentinel selecting the context-less signal bucket.
UNATTRIBUTED_SCOPE = "unattributed"


class MemoryHealthNote(BaseModel):
    """One structured section note (#1225).

    Attributes:
        code: Stable note code the frontend maps to a localized string
            (the code → rationale mapping lives in
            docs/ops/memory-health-report.md).
        params: Interpolation values for the localized message.
    """

    code: str
    params: dict[str, Any] = Field(default_factory=dict)


class MemoryHealthSection(BaseModel):
    """One graded health section.

    Attributes:
        status: ok | warn | fail (fail only on deterministic facts).
        metrics: Label-free numeric signals backing the grade.
        notes: Structured explanations for every non-ok contribution.
    """

    status: HealthStatus
    metrics: dict[str, Any]
    notes: list[MemoryHealthNote]


class MemoryHealthContextEntry(BaseModel):
    """One breakdown row: a single context's graded statuses.

    Attributes:
        context_id: The context UUID, or null for the unattributed bucket
            (signals recorded without a context).
        name: Context display name; null for the unattributed bucket.
        overall_status: Worst section status for this context.
        sections: Per-section status only — metrics live in the detail view.
    """

    context_id: str | None
    name: str | None
    overall_status: HealthStatus
    sections: dict[str, HealthStatus]


class MemoryHealthBreakdownResponse(BaseModel):
    """200 response for GET /admin/memory-health (no context_id)."""

    generated_at: str
    overall_status: HealthStatus
    contexts: list[MemoryHealthContextEntry]


class MemoryHealthDetailResponse(BaseModel):
    """200 response for GET /admin/memory-health?context_id=..."""

    generated_at: str
    context_id: str | None
    context_name: str | None
    overall_status: HealthStatus
    sections: dict[str, MemoryHealthSection]


@router.get(
    "",
    response_model=MemoryHealthBreakdownResponse | MemoryHealthDetailResponse,
    summary="Per-context memory-health report (self-diagnosis)",
)
async def get_memory_health(
    admin: AdminUser,
    context_id: Annotated[
        uuid.UUID | Literal["unattributed"] | None,
        Query(
            description=(
                "Omit for the per-context breakdown. Pass a context UUID for "
                "that context's 3-section detail (must be owned by the caller "
                "— otherwise 404), or the sentinel 'unattributed' for signals "
                "recorded without a context."
            ),
        ),
    ] = None,
    db: AsyncSession = Depends(get_db),
) -> MemoryHealthBreakdownResponse | MemoryHealthDetailResponse:
    """Build the thresholded health document(s) for the calling admin.

    Sections: consolidation (judge health, merge audit, soft-delete
    backlog), graph (edge composition, weight invariant, cold-graph
    check), retrieval (usage volume, config posture). Label-free signals
    only — gold-label rates (stale_only, P@k) live in the #1210 eval
    gates, not here.
    """
    user_id = admin.get("user_id")
    if not user_id:
        raise MemoryCloudException(
            message="Admin user id missing from session.",
            status_code=500,
            error_code="HEALTH-001",
        )

    service = MemoryHealthService(db)

    if context_id is None:
        breakdown = await service.build_breakdown(user_id)
        return MemoryHealthBreakdownResponse(**breakdown)

    # FastAPI already validated the param shape (UUID | 'unattributed'):
    # anything else got the standard 422 body before reaching here.
    scope = None if context_id == UNATTRIBUTED_SCOPE else context_id

    report = await service.build_context_report(user_id, scope)
    if report is None:
        # Uniform 404: no distinction between "does not exist" and
        # "not owned by the caller" (CWE-639 discipline).
        raise HTTPException(status_code=404, detail="Context not found")
    return MemoryHealthDetailResponse(**report)
