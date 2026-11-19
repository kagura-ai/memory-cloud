"""The workspace slot gate for MCP tool calls that name no target (#2918).

A call that names its target (``context_id`` / ``context_ids``, a
``workspace_id``, a ``file_id`` or a Sleep ``report_id``) is checked after
authorization by the resolver that reaches that target (``_resolve_context``,
``_resolve_context_for_read``, ``_get_workspace_member_role``, the permission
primitives), so a context in an active workspace keeps working while the
session workspace is suspended, and a context in a suspended workspace is
refused from an active session (target-based, as #1954 does for the capacity
lock). A call that names no target works on the caller's session workspace:
it is checked here, before the handler, and the check fails closed.

The manuals (``guide``, ``describe_tools``) answer whatever the workspace's
state: they read no workspace data.
"""

from __future__ import annotations

from contextlib import aclosing
from typing import Any
from uuid import UUID

from mcp_server.tools._capacity_gate import names_a_target
from mcp_server.tools._errors import describe_tool_exception
from mcp_server.tools._helpers import ToolErrorContent
from services.workspace_slots import workspace_slots_enforced
from utils.logger import get_logger

logger = get_logger(__name__)

# Tools that never read or write workspace data: callable on a suspended
# session workspace.
SLOT_GATE_EXEMPT_TOOLS: frozenset[str] = frozenset({"guide", "describe_tools"})

# Target-less tools whose handlers check membership of the session workspace
# and then the slot gate themselves (``_check_workspace_membership``): the
# dispatcher leaves them to it, so a caller who is not a member gets the
# membership answer whether the workspace is suspended or not.
HANDLER_GATED_TOOLS: frozenset[str] = frozenset(
    {"get_usage", "get_resource_impact", "get_resource_schema"}
)


async def slot_suspension_refusal(
    tool_name: str,
    args: dict[str, Any],
    user_id: str,
    workspace_id: UUID | None,
) -> ToolErrorContent | None:
    """The ``workspace_suspended`` envelope for a target-less call on a suspended session workspace.

    ``None`` (go on) for an exempt tool, a tool whose handler gates after its
    own membership check (``HANDLER_GATED_TOOLS``), a call that names its
    target (the resolvers check that target), a call with no session
    workspace, and
    whenever the mode is not ``enforce`` — then without opening a session.
    Fails closed: an error while checking refuses the call
    (``service_unavailable`` / ``internal_error`` with a correlation id).
    """
    if tool_name in SLOT_GATE_EXEMPT_TOOLS or tool_name in HANDLER_GATED_TOOLS:
        return None
    if workspace_id is None:
        return None
    if not workspace_slots_enforced() or names_a_target(args):
        return None

    from db.base import get_db
    from services.workspace_slot_gate import ensure_slot_active

    try:
        # aclosing: an early return / raise releases the session at once.
        async with aclosing(get_db()) as sessions:
            async for db in sessions:
                await ensure_slot_active(db, workspace_id, user_id=user_id)
                return None
    except Exception as exc:  # noqa: BLE001 — the refusal, or fail closed
        return describe_tool_exception(tool_name, exc).response()
    return None  # pragma: no cover - get_db always yields
