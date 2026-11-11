"""Deprecated alias: GET /api/v1/admin/memory-health (#1211, #2764).

#2764 moved the memory-health report to ``GET /api/v1/memory-health``
(``api.routes.memory_health``), served to any signed-in user for the contexts
they created. This path stays for one release as a deprecated alias: still
behind the system-admin gate, answering with the same document by calling the
same handler. A later release removes it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import memory_health
from api.routes.memory_health import (
    ContextScopeQuery,
    MemoryHealthBreakdownResponse,
    MemoryHealthDetailResponse,
)
from auth.dependencies import AdminUser
from db.base import get_db

router = APIRouter(prefix="/admin/memory-health", tags=["admin-memory-health"])


@router.get(
    "",
    response_model=MemoryHealthBreakdownResponse | MemoryHealthDetailResponse,
    summary="Per-context memory-health report (deprecated alias)",
    description=(
        "Deprecated: use GET /api/v1/memory-health, which answers any signed-in "
        "user with the same document. This system-admin alias is removed in a "
        "later release."
    ),
    deprecated=True,
)
async def get_memory_health(
    admin: AdminUser,
    context_id: ContextScopeQuery = None,
    db: AsyncSession = Depends(get_db),
) -> MemoryHealthBreakdownResponse | MemoryHealthDetailResponse:
    """Answer with the handler of ``GET /api/v1/memory-health`` for the admin."""
    return await memory_health.get_memory_health(admin, context_id, db)
