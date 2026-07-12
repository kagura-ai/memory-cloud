"""Consolidated memory-health report API (#1211).

One endpoint answering "is this partition's memory healthy?" from the
signals the system already emits (sleep reports, graph invariants, usage
stats, config posture) — thresholded ok/warn/fail per section so a
silent-judge-death class of failure (#1177) surfaces without an external
eval program. Self-scoped like the manual sleep trigger (Phase 1).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from db.base import get_db
from services.memory_health_service import MemoryHealthService
from utils.exceptions import MemoryCloudException
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/memory-health", tags=["admin-memory-health"])


HealthStatus = Literal["ok", "warn", "fail"]


class MemoryHealthSection(BaseModel):
    """One graded health section.

    Attributes:
        status: ok | warn | fail (fail only on deterministic facts).
        metrics: Label-free numeric signals backing the grade.
        notes: Human-readable explanations for every non-ok contribution.
    """

    status: HealthStatus
    metrics: dict[str, Any]
    notes: list[str]


class MemoryHealthResponse(BaseModel):
    """200 response for GET /admin/memory-health."""

    generated_at: str
    overall_status: HealthStatus
    sections: dict[str, MemoryHealthSection]


@router.get(
    "",
    response_model=MemoryHealthResponse,
    summary="Consolidated memory-health report (self-diagnosis)",
)
async def get_memory_health(
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> MemoryHealthResponse:
    """Build the thresholded health document for the calling admin's data.

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

    report = await MemoryHealthService(db).build_report(user_id)
    return MemoryHealthResponse(**report)
