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

import time
from contextlib import aclosing
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

# BLOCKED tools whose handler reaches a service that checks the lock itself,
# AFTER authorization (MemoryService read/write paths, FileStorageService
# uploads, ContextService.create_context). For a target named in the
# arguments the dispatcher leaves these to that post-authorization check, so
# a caller who may not reach the target gets the handler's not-found and the
# lock is no existence or billing-state oracle (CSO F3).
CAPACITY_LOCK_SERVICE_GATED_TOOLS: frozenset[str] = frozenset(
    {
        "recall",
        "reference",
        "explore",
        "load_pinned",
        "remember",
        "remember_batch",
        "update_memory",
        "create_context",
        "init_file_upload",
        "complete_file_upload",
    }
)

# BLOCKED tools with NO service-level second line: the dispatcher is their
# only enforcement, for argument-derived and session workspaces alike.
# ``bootstrap`` / ``get_agent_bootstrap`` are here although they call
# load_pinned/recall: their components are fail-soft, so a service refusal
# would only blank one lane while the others (upcoming, state, …) answered.
CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS: frozenset[str] = (
    CAPACITY_LOCK_BLOCKED_TOOLS - CAPACITY_LOCK_SERVICE_GATED_TOOLS
)

# "This workspace is not a lock candidate" (paid, self-hosted, admin-managed
# Free), cached per session workspace so a blocked call on such a workspace
# opens no database session at all. Same shape as ``_RATE_LIMIT_CACHE``. There
# is no production hook that invalidates on a plan push (the entitlement PUT
# does not call ``invalidate_rate_limit_cache`` either), so the short TTL
# bounds how long a workspace that just returned to Free goes unchecked here;
# the service-level checks are unaffected.
#
# Keyed by ``(kind, id)``: ``("ws", workspace_id)`` for a session or named
# workspace, ``("ctx", context_id)`` for a context named in the arguments — a
# dispatcher-only tool called with a context of a paid workspace then costs no
# session either after its first call. A context is cached only when the whole
# call resolved to no candidate at all.
_NOT_CANDIDATE_CACHE: dict[tuple[str, UUID], float] = {}
_NOT_CANDIDATE_TTL = 30.0  # seconds
_NOT_CANDIDATE_CACHE_MAX_SIZE = 10_000


def _known_not_candidate(target_id: UUID, kind: str = "ws") -> bool:
    key = (kind, target_id)
    expires = _NOT_CANDIDATE_CACHE.get(key)
    if expires is None:
        return False
    if expires <= time.monotonic():
        _NOT_CANDIDATE_CACHE.pop(key, None)
        return False
    return True


def _remember_not_candidate(target_id: UUID, kind: str = "ws") -> None:
    now = time.monotonic()
    if len(_NOT_CANDIDATE_CACHE) >= _NOT_CANDIDATE_CACHE_MAX_SIZE:
        for key in [k for k, v in _NOT_CANDIDATE_CACHE.items() if v <= now]:
            del _NOT_CANDIDATE_CACHE[key]
        if len(_NOT_CANDIDATE_CACHE) >= _NOT_CANDIDATE_CACHE_MAX_SIZE:
            oldest = min(_NOT_CANDIDATE_CACHE, key=lambda k: _NOT_CANDIDATE_CACHE[k])
            del _NOT_CANDIDATE_CACHE[oldest]
    _NOT_CANDIDATE_CACHE[(kind, target_id)] = now + _NOT_CANDIDATE_TTL


def invalidate_capacity_candidate_cache(workspace_id: UUID | None = None) -> None:
    """Drop cached "not a candidate" answers (tests, admin tooling)."""
    if workspace_id is None:
        _NOT_CANDIDATE_CACHE.clear()
    else:
        _NOT_CANDIDATE_CACHE.pop(("ws", workspace_id), None)


# Arguments naming a context whose workspace is the target. ``source_id`` /
# ``target_id`` are NOT here: in create_edge / update_edge they are memory ids,
# and those tools carry ``context_id`` as well. merge_contexts names its two
# contexts ``source_context_id`` / ``target_context_id``. A test checks every
# name against the BLOCKED tools' input schemas.
_CONTEXT_ARGS = (
    "context_id",
    "source_context_id",
    "target_context_id",
)
_CONTEXT_LIST_ARGS = ("context_ids",)
_WORKSPACE_ARGS = ("workspace_id",)
_FILE_ARGS = ("file_id",)


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
    for key in _CONTEXT_LIST_ARGS:
        many = args.get(key)
        if isinstance(many, list):
            ids.update(cid for v in many if (cid := _uuid(v)) is not None)
    return ids


def _argument_targets(args: dict[str, Any]) -> tuple[set[UUID], UUID | None, UUID | None]:
    """``(context ids, workspace_id, file_id)`` the arguments name."""
    return _target_context_ids(args), _uuid(args.get("workspace_id")), _uuid(args.get("file_id"))


async def _candidate_workspaces(db: Any, where: Any) -> list[Any]:
    """Lock-candidate workspaces matching ``where`` — one query, filtered in SQL."""
    from models.auth import Workspace
    from services.capacity_lock import lock_candidate_predicate

    rows = await db.scalars(
        select(Workspace).where(where, lock_candidate_predicate()).order_by(Workspace.id)
    )
    return list(rows.all())


def _argument_target_clause(
    context_ids: set[UUID], workspace_arg: UUID | None, file_id: UUID | None
) -> Any:
    from sqlalchemy import or_

    from models.auth import Context, Workspace
    from models.file_objects import FileObject

    clauses = []
    if context_ids:
        clauses.append(
            Workspace.id.in_(
                select(Context.workspace_id).where(
                    Context.id.in_(context_ids), Context.deleted_at.is_(None)
                )
            )
        )
    if workspace_arg is not None:
        clauses.append(Workspace.id == workspace_arg)
    if file_id is not None:
        clauses.append(
            Workspace.id.in_(select(FileObject.workspace_id).where(FileObject.id == file_id))
        )
    return or_(*clauses)


async def _names_public_context(db: Any, workspace_id: UUID, context_ids: set[UUID]) -> bool:
    """Whether the call names a live public context of ``workspace_id``."""
    if not context_ids:
        return False
    from sqlalchemy import exists

    from models.auth import Context

    found = await db.scalar(
        select(
            exists().where(
                Context.id.in_(context_ids),
                Context.workspace_id == workspace_id,
                Context.is_public.is_(True),
                Context.deleted_at.is_(None),
            )
        )
    )
    return bool(found)


async def capacity_lock_refusal(
    tool_name: str,
    args: dict[str, Any],
    user_id: str,
    workspace_id: UUID | None,
) -> ToolErrorContent | None:
    """The ``capacity_locked`` envelope when a blocked tool targets a locked workspace.

    * Target named in the arguments: only for
      ``CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS``, and only when the caller is a
      member of that workspace (else the handler's not-found answers). The
      service-gated tools are left to their post-authorization check.
    * No target in the arguments: the session workspace (the caller's own) is
      enforced, skipping the database entirely while it is cached as not a
      lock candidate.

    Fails open on an infrastructure error (like the rate-limit check).
    """
    if tool_name not in CAPACITY_LOCK_BLOCKED_TOOLS:
        return None

    context_ids, workspace_arg, file_id = _argument_targets(args)
    from_args = bool(context_ids or workspace_arg or file_id)
    if from_args and tool_name not in CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS:
        return None
    if not from_args and (workspace_id is None or _known_not_candidate(workspace_id)):
        return None
    if (
        from_args
        and file_id is None
        and all(_known_not_candidate(cid, "ctx") for cid in context_ids)
        and (workspace_arg is None or _known_not_candidate(workspace_arg))
    ):
        return None

    from db.base import get_db
    from models.auth import Workspace
    from services.capacity_lock import capacity_lock_state, is_workspace_member

    try:
        # aclosing: an early return / raise releases the session at once
        # instead of leaving the generator (and its connection) to the GC.
        async with aclosing(get_db()) as sessions:
            async for db in sessions:
                if from_args:
                    where = _argument_target_clause(context_ids, workspace_arg, file_id)
                else:
                    where = Workspace.id == workspace_id
                workspaces = await _candidate_workspaces(db, where)
                if not workspaces:
                    if not from_args and workspace_id is not None:
                        _remember_not_candidate(workspace_id)
                    for cid in context_ids:
                        _remember_not_candidate(cid, "ctx")
                    if workspace_arg is not None:
                        _remember_not_candidate(workspace_arg)
                for ws in workspaces:
                    lock = await capacity_lock_state(db, ws)
                    if lock is None:
                        continue
                    if from_args and ws.id != workspace_id:
                        if not await is_workspace_member(db, ws.id, user_id):
                            # A non-member reaches a locked workspace only
                            # through one of its PUBLIC contexts — refuse that
                            # in the redacted form (as the REST public and
                            # graph routes do). Anything else is left to the
                            # handler's own not-found: no existence oracle.
                            if await _names_public_context(db, ws.id, context_ids):
                                raise CapacityLockedError.for_outsider()
                            continue
                    raise lock.to_error()
                return None
    except CapacityLockedError as exc:
        return describe_tool_exception(tool_name, exc).response()
    except Exception as exc:  # noqa: BLE001 — fail open, logged
        logger.warning("capacity_lock_check_failed", tool=tool_name, error=str(exc))
    return None
