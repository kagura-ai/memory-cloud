"""Capacity-over lock classification for the MCP tools (#1941, #1954).

Every registered tool is classified here, once, as BLOCKED or ALLOWED while
the target workspace is over its Free capacity (``services.capacity_lock``).
``tests/mcp_server/test_capacity_gate.py`` fails when a tool is in neither
set or in both, so a new tool cannot ship without deciding.

The rule: a locked workspace keeps everything it needs to get back under the
cap — listing, deleting, usage, the manuals, export — and loses search and
saving. A content reader is BLOCKED (the lock exists because reading paid-plan
data on Free is the paid plan's value); pure metadata is ALLOWED.

Where a BLOCKED tool is checked (#1954; the three sets partition BLOCKED,
pinned by ``tests/mcp_server/test_capacity_resolver_gate.py``):

* ``CAPACITY_LOCK_SERVICE_GATED_TOOLS`` — by the service the handler calls,
  after authorization.
* ``CAPACITY_LOCK_RESOLVER_GATED_TOOLS`` — by the handler's context resolver,
  after authorization: ``capacity_policy_for`` is BLOCKED for these, and the
  dispatcher hands it to the resolvers through ``_helpers.mcp_capacity_policy``.
* ``CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS`` — here, on the caller's session
  workspace, before the handler (``capacity_lock_refusal``).

A service- or resolver-gated call that names no target in its arguments is
also checked here on the session workspace, as before #1954.
"""

from __future__ import annotations

import time
from contextlib import aclosing
from typing import Any
from uuid import UUID

from sqlalchemy import select

from mcp_server.tools._errors import describe_tool_exception
from mcp_server.tools._helpers import ToolErrorContent
from services.capacity_lock import CapacityPolicy
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
        # #2043: a memory's edit history (the fields each edit changed);
        # parity with reference, which reads the earlier versions' text.
        "list_revisions",
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
        # #2044: un-forgets and restores earlier values — adds data back, and
        # its dry run lists the session's memories (parity with the above).
        "rollback_session",
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
# uploads, ContextService.create_context, AgentBootstrapService.resolve_context).
# Their resolvers run with ALLOWED (``capacity_policy_for``): the service's
# check is the one that answers.
CAPACITY_LOCK_SERVICE_GATED_TOOLS: frozenset[str] = frozenset(
    {
        "recall",
        "reference",
        "list_revisions",  # MemoryService.list_revisions checks after access
        "explore",
        "load_pinned",
        "remember",
        "remember_batch",
        "update_memory",
        "create_context",
        "init_file_upload",
        "complete_file_upload",
        # The service resolves the agent's context with BLOCKED (#1954), before
        # any fail-soft component runs.
        "get_agent_bootstrap",
    }
)

# BLOCKED tools whose handler resolves its target context through a resolver
# that checks the lock after authorization (#1954): ``_resolve_context`` /
# ``_resolve_context_for_read`` read the running tool's policy, and
# update_search_config passes it to ``check_context_write``. A caller who may
# not reach the context gets the handler's not-found (or its own refusal)
# instead of the lock.
CAPACITY_LOCK_RESOLVER_GATED_TOOLS: frozenset[str] = frozenset(
    {
        "bootstrap",
        "changes_since",
        "create_edge",
        "feedback",
        "get_sleep_history",
        "get_state",
        "list_edges",
        "merge_contexts",
        "recall_nearby",
        "recall_series",
        "recall_upcoming",
        "record_measurement",
        "rollback_session",
        "set_state",
        "update_edge",
        "update_search_config",
    }
)

# BLOCKED tools checked only here, on the caller's session workspace — the
# check runs before authorization and tells nothing about another workspace.
# The analysis and resource / connector handlers confine their target to the
# session workspace, so this is the target's workspace. The sleep report tools
# take a report id (filtered on the caller's user id, not on a workspace) and
# are checked on the session workspace, exactly as before #1954.
CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS: frozenset[str] = (
    CAPACITY_LOCK_BLOCKED_TOOLS
    - CAPACITY_LOCK_SERVICE_GATED_TOOLS
    - CAPACITY_LOCK_RESOLVER_GATED_TOOLS
)


def capacity_policy_for(tool_name: str) -> CapacityPolicy:
    """The capacity policy the context resolvers apply while ``tool_name`` runs.

    BLOCKED only for ``CAPACITY_LOCK_RESOLVER_GATED_TOOLS``. A service-gated
    tool resolves with ALLOWED (its service checks after authorization — no
    second COUNT+SUM per call on a lock-candidate workspace), and an ALLOWED
    tool is never locked.
    """
    if tool_name in CAPACITY_LOCK_RESOLVER_GATED_TOOLS:
        return CapacityPolicy.BLOCKED
    return CapacityPolicy.ALLOWED


# "This workspace is not a lock candidate" (paid, self-hosted, admin-managed
# Free), cached per session workspace so a blocked call on such a workspace
# opens no database session at all. Same shape as ``_RATE_LIMIT_CACHE``. There
# is no production hook that invalidates on a plan push (the entitlement PUT
# does not call ``invalidate_rate_limit_cache`` either), so the short TTL
# bounds how long a workspace that just returned to Free goes unchecked here.
# Only this pre-authorization check reads it: the resolvers and services check
# after authorization without a cache (#1954), so a stale answer never reaches
# them.
_NOT_CANDIDATE_CACHE: dict[UUID, float] = {}
_NOT_CANDIDATE_TTL = 30.0  # seconds
_NOT_CANDIDATE_CACHE_MAX_SIZE = 10_000


def _known_not_candidate(workspace_id: UUID) -> bool:
    expires = _NOT_CANDIDATE_CACHE.get(workspace_id)
    if expires is None:
        return False
    if expires <= time.monotonic():
        _NOT_CANDIDATE_CACHE.pop(workspace_id, None)
        return False
    return True


def _remember_not_candidate(workspace_id: UUID) -> None:
    now = time.monotonic()
    if len(_NOT_CANDIDATE_CACHE) >= _NOT_CANDIDATE_CACHE_MAX_SIZE:
        for key in [k for k, v in _NOT_CANDIDATE_CACHE.items() if v <= now]:
            del _NOT_CANDIDATE_CACHE[key]
        if len(_NOT_CANDIDATE_CACHE) >= _NOT_CANDIDATE_CACHE_MAX_SIZE:
            oldest = min(_NOT_CANDIDATE_CACHE, key=lambda k: _NOT_CANDIDATE_CACHE[k])
            del _NOT_CANDIDATE_CACHE[oldest]
    _NOT_CANDIDATE_CACHE[workspace_id] = now + _NOT_CANDIDATE_TTL


def invalidate_capacity_candidate_cache(workspace_id: UUID | None = None) -> None:
    """Drop cached "not a candidate" answers (tests, admin tooling)."""
    if workspace_id is None:
        _NOT_CANDIDATE_CACHE.clear()
    else:
        _NOT_CANDIDATE_CACHE.pop(workspace_id, None)


# Arguments naming a target the handler resolves itself. ``source_id`` /
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


def _names_a_target(args: dict[str, Any]) -> bool:
    context_ids, workspace_arg, file_id = _argument_targets(args)
    return bool(context_ids) or workspace_arg is not None or file_id is not None


async def _candidate_workspaces(db: Any, where: Any) -> list[Any]:
    """Lock-candidate workspaces matching ``where`` — one query, filtered in SQL."""
    from models.auth import Workspace
    from services.capacity_lock import lock_candidate_predicate

    rows = await db.scalars(
        select(Workspace).where(where, lock_candidate_predicate()).order_by(Workspace.id)
    )
    return list(rows.all())


async def capacity_lock_refusal(
    tool_name: str,
    args: dict[str, Any],
    workspace_id: UUID | None,
) -> ToolErrorContent | None:
    """The ``capacity_locked`` envelope when a blocked tool runs on a locked session workspace.

    * A service- or resolver-gated tool whose arguments name its target: no
      check here — the check after authorization answers, so a caller who may
      not reach the target gets the handler's not-found (#1954).
    * Every other blocked call: the session workspace (the caller's own) is
      enforced, skipping the database entirely while it is cached as not a
      lock candidate. A dispatcher-only tool never looks at the workspace of
      an id in its arguments.

    Fails open on an infrastructure error (like the rate-limit check).
    """
    if tool_name not in CAPACITY_LOCK_BLOCKED_TOOLS:
        return None
    if tool_name not in CAPACITY_LOCK_DISPATCHER_ONLY_TOOLS and _names_a_target(args):
        return None
    if workspace_id is None or _known_not_candidate(workspace_id):
        return None

    from db.base import get_db
    from models.auth import Workspace
    from services.capacity_lock import capacity_lock_state

    try:
        # aclosing: an early return / raise releases the session at once
        # instead of leaving the generator (and its connection) to the GC.
        async with aclosing(get_db()) as sessions:
            async for db in sessions:
                workspaces = await _candidate_workspaces(db, Workspace.id == workspace_id)
                if not workspaces:
                    _remember_not_candidate(workspace_id)
                for ws in workspaces:
                    lock = await capacity_lock_state(db, ws)
                    if lock is not None:
                        raise lock.to_error()
                return None
    except CapacityLockedError as exc:
        return describe_tool_exception(tool_name, exc).response()
    except Exception as exc:  # noqa: BLE001 — fail open, logged
        logger.warning("capacity_lock_check_failed", tool=tool_name, error=str(exc))
    return None
