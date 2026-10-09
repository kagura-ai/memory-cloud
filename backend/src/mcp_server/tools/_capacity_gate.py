"""Capacity-over lock at the MCP dispatcher (#1941).

Every registered tool is classified here, once, as BLOCKED or ALLOWED while
the target workspace is over its Free capacity (``services.capacity_lock``).
``tests/mcp_server/test_capacity_gate.py`` fails when a tool is in neither
set or in both, so a new tool cannot ship without deciding.

The rule: a locked workspace keeps everything it needs to get back under the
cap — listing, deleting, usage, the manuals, export — and loses search and
saving. A content reader is BLOCKED (the lock exists because reading paid-plan
data on Free is the paid plan's value); pure metadata is ALLOWED.

The service layer checks again on the memory read/write paths, so a tool that
reaches ``MemoryService`` is refused even if it were misclassified here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from mcp_server.tools._errors import describe_tool_exception
from mcp_server.tools._helpers import ToolErrorContent
from utils.exceptions import CapacityLockedError
from utils.logger import get_logger

logger = get_logger(__name__)

CAPACITY_LOCK_BLOCKED_TOOLS: frozenset[str] = frozenset(
    {
        # ---- content reads ------------------------------------------------
        "recall",
        "recall_upcoming",
        "recall_nearby",
        "recall_series",
        "reference",
        "explore",
        "load_pinned",
        "bootstrap",
        "get_agent_bootstrap",
        "get_state",
        # Returns the changed memories' summaries — a content feed, unlike
        # ``list``, which is the cleanup surface.
        "changes_since",
        # The memory graph and the sleep / analysis outputs describe memory
        # content (relations, cluster labels, consolidation summaries) and are
        # not needed to clean up.
        "list_edges",
        "get_sleep_history",
        "get_sleep_report",
        "get_analysis",
        "get_active_analysis",
        "list_analyses",
        "get_cluster",
        # ---- writes -------------------------------------------------------
        "remember",
        "remember_batch",
        "update_memory",
        "set_state",
        "record_measurement",
        "feedback",
        "create_edge",
        "update_edge",
        "create_context",
        "merge_contexts",
        "analyze_context",
        "init_file_upload",
        "complete_file_upload",
        "setup_resource",
        "setup_connector",
        "ingest_events",
        "update_search_config",
        # Restores memories a sleep run consolidated — it adds data back.
        "rollback_sleep_run",
    }
)

CAPACITY_LOCK_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        # ---- the cleanup surface -----------------------------------------
        "list",
        "list_contexts",
        "get_context_info",
        "list_tags",
        "list_files",
        "forget",
        "delete_context",
        "delete_file",
        "delete_edge",
        # Downloading a stored file is export, which stays allowed (the REST
        # download route is not gated either): the owner must be able to keep
        # a copy before deleting it.
        "get_file_download_url",
        # Guardrails stay available while locked: they are safety rails (what
        # a client must not do), small and bounded, and the same entries are
        # already served by get_context_info's guardrails field, REST
        # /memory/guardrails/digest and the MCP session instructions. Pausing
        # one door while the others answer would protect nothing.
        "load_guardrails",
        # ---- usage and manuals --------------------------------------------
        "get_usage",
        "guide",
        "describe_tools",
        # ---- metadata, no memory content and no capacity -----------------
        # Rename / describe / privacy of an existing context.
        "update_context",
        "list_my_bindings",
        "describe_binding",
        "register_agent",
        "list_agents",
        "get_agent",
        "update_agent",
        "delete_agent",
        "bind_agent_context",
        "list_agent_bindings",
        "update_agent_binding",
        "unbind_agent_context",
        "list_resource_tokens",
        "get_resource_impact",
        "get_resource_schema",
        # The secret store is on every plan and is not memory or file
        # capacity; an agent must still be able to fetch its deploy key.
        "secret_register_pubkey",
        "secret_put",
        "secret_get",
        "secret_list",
        "secret_revoke_grant",
    }
)

# Arguments naming a context whose workspace is the target.
_CONTEXT_ARGS = (
    "context_id",
    "source_context_id",
    "source_id",
    "target_context_id",
    "target_id",
)


def _uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _target_context_ids(args: dict[str, Any]) -> set[UUID]:
    ids = {cid for key in _CONTEXT_ARGS if (cid := _uuid(args.get(key))) is not None}
    many = args.get("context_ids")
    if isinstance(many, list):
        ids.update(cid for v in many if (cid := _uuid(v)) is not None)
    return ids


async def _target_workspace_ids(db: Any, args: dict[str, Any]) -> set[UUID]:
    """The workspaces a call reads or writes, from its arguments.

    Unknown ids are skipped: the handler owns not-found, and the gate must
    not answer differently for an id that does not exist.
    """
    from models.auth import Context
    from models.file_objects import FileObject

    found: set[UUID] = set()
    context_ids = _target_context_ids(args)
    if context_ids:
        rows = await db.scalars(
            select(Context.workspace_id).where(
                Context.id.in_(context_ids), Context.deleted_at.is_(None)
            )
        )
        found.update(rows)
    if (ws := _uuid(args.get("workspace_id"))) is not None:
        found.add(ws)
    if (file_id := _uuid(args.get("file_id"))) is not None:
        ws_of_file = await db.scalar(
            select(FileObject.workspace_id).where(FileObject.id == file_id)
        )
        if ws_of_file is not None:
            found.add(ws_of_file)
    return found


async def capacity_lock_refusal(
    tool_name: str,
    args: dict[str, Any],
    user_id: str,
    workspace_id: UUID | None,
) -> ToolErrorContent | None:
    """The ``capacity_locked`` envelope when a blocked tool targets a locked workspace.

    Fails open on an infrastructure error (like the rate-limit check): the
    service-layer check is the second line for the memory paths.
    """
    if tool_name not in CAPACITY_LOCK_BLOCKED_TOOLS:
        return None

    from db.base import get_db
    from services.capacity_lock import ensure_not_capacity_locked

    try:
        async for db in get_db():
            targets = await _target_workspace_ids(db, args)
            # The session workspace is authenticated, so its own numbers are
            # shown. A workspace reached through an argument is checked with
            # the caller's id so a non-member sees no counts.
            if workspace_id is not None and (not targets or workspace_id in targets):
                await ensure_not_capacity_locked(db, workspace_id)
            for ws in sorted(targets - {workspace_id}, key=str):
                await ensure_not_capacity_locked(db, ws, user_id=user_id)
            return None
    except CapacityLockedError as exc:
        return describe_tool_exception(tool_name, exc).response()
    except Exception as exc:  # noqa: BLE001 — fail open, logged
        logger.warning("capacity_lock_check_failed", tool=tool_name, error=str(exc))
    return None
