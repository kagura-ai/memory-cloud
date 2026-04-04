"""MCP tool handler for get_usage — quota and usage query."""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import _error_response, _log_tool_usage, _success_response

logger = logging.getLogger(__name__)


async def handle_get_usage(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Get current workspace usage and quota limits.

    Returns memory, context, and member usage with effective limits.
    """
    from sqlalchemy import func, select

    from db.base import get_db
    from models.auth import Context, Workspace, WorkspaceMember
    from models.memory import Memory
    from services.effective_quota_service import EffectiveQuotaService
    from services.quota_service import QuotaService

    start_time = time.time()

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace")

    async for db in get_db():
        try:
            ws_result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
            workspace = ws_result.scalar_one_or_none()

            if not workspace:
                return _error_response("workspace_not_found", f"Workspace {workspace_id} not found")

            mem_result = await db.execute(
                select(func.count(Memory.id)).where(
                    Memory.workspace_id == workspace_id,
                    Memory.deleted_at.is_(None),
                )
            )
            memory_count = mem_result.scalar() or 0

            ctx_result = await db.execute(
                select(func.count(Context.id)).where(
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                )
            )
            context_count = ctx_result.scalar() or 0

            member_result = await db.execute(
                select(func.count(WorkspaceMember.id)).where(
                    WorkspaceMember.workspace_id == workspace_id
                )
            )
            member_count = member_result.scalar() or 0

            quotas = await EffectiveQuotaService(db).get_effective_quotas(workspace_id)
            effective_memory = quotas["memory_limit"]
            memory_pct = (
                round(memory_count / effective_memory * 100, 1) if effective_memory > 0 else 0
            )

            mcp_used_today = await QuotaService(db).count_mcp_calls_today(workspace_id)

            await _log_tool_usage(db, user_id, "get_usage", start_time, 200, None, workspace_id)

            return _success_response(
                plan=workspace.plan_name,
                memories={
                    "used": memory_count,
                    "limit": effective_memory,
                    "percentage": memory_pct,
                },
                contexts={
                    "used": context_count,
                    "limit": quotas["max_contexts"],
                },
                members={
                    "used": member_count,
                    "limit": quotas["max_members"],
                },
                mcp_calls_per_day={
                    "used": mcp_used_today,
                    "limit": quotas["mcp_calls_per_day"],
                },
            )

        except Exception as e:
            await db.rollback()
            logger.error(f"get_usage_failed: {e}", exc_info=True)
            await _log_tool_usage(db, user_id, "get_usage", start_time, 500, None, workspace_id)
            return _error_response("get_usage_error", str(e))

    return _error_response("internal_error", "Failed to acquire database session")
