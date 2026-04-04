"""MCP tool handler for get_usage — quota and usage query.

Issue #82: Allow agents to proactively check quota before operations.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import _error_response, _log_tool_usage


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

    start_time = time.time()

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace")

    async with get_db() as db:
        # Get workspace
        ws_result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
        workspace = ws_result.scalar_one_or_none()

        if not workspace:
            return _error_response("workspace_not_found", f"Workspace {workspace_id} not found")

        # Memory count (workspace-scoped)
        mem_result = await db.execute(
            select(func.count(Memory.id)).where(
                Memory.workspace_id == workspace_id,
                Memory.deleted_at.is_(None),
            )
        )
        memory_count = mem_result.scalar() or 0

        # Context count
        ctx_result = await db.execute(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
        context_count = ctx_result.scalar() or 0

        # Member count
        member_result = await db.execute(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
        member_count = member_result.scalar() or 0

        # Build response using effective properties
        effective_memory = workspace.effective_memory_limit
        memory_pct = round(memory_count / effective_memory * 100, 1) if effective_memory > 0 else 0

        response = {
            "status": "success",
            "plan": workspace.plan_name,
            "memories": {
                "used": memory_count,
                "limit": effective_memory,
                "percentage": memory_pct,
            },
            "contexts": {
                "used": context_count,
                "limit": workspace.effective_max_contexts,
            },
            "members": {
                "used": member_count,
                "limit": workspace.effective_max_members,
            },
            "mcp_calls_per_day": {
                "limit": workspace.effective_mcp_calls_per_day,
            },
        }

        await _log_tool_usage(db, user_id, "get_usage", start_time, 200, None, workspace_id)
        await db.commit()

        return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False))]
