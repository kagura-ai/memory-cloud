"""MCP tool handlers for memory broadlistening analyses (Issue #496).

Five tools that mirror the REST endpoints in
``api/routes/analyses.py`` — same gate chain, same data flow, same
error envelope where possible — so MCP and REST behave identically
(#332 教訓: shared composition helper, no duplicated logic).

Tools:

- ``analyze_context``       — POST /analyses (or preview when dry_run=true)
- ``get_analysis``          — GET /analyses/{run_id}
- ``list_analyses``         — GET /analyses
- ``get_active_analysis``   — GET /analyses/active
- ``get_cluster``           — drill-down (Layer 1+2 + paginated memories)

Gate handling: all 5 call into ``auth.analysis_gates`` for the same
4-stage chain (owner + Pro tier + quota + allowlist) that the REST
routes use. Read-only tools pass ``require_quota=False`` so a Pro user
who exhausted today's quota can still inspect past runs.

Output shape: every handler emits ``_success_response(...)`` or
``_error_response(...)`` from ``_helpers.py`` so MCP clients see the
same envelope as every other tool in this codebase.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _error_response,
    _log_tool_usage,
    _success_response,
)
from utils.exceptions import (
    AuthorizationError,
    ConflictError,
    FeatureNotAvailableError,
    NotFoundException,
    QuotaExceededError,
    ValidationError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Strong-ref set for ``asyncio.create_task`` — without this the task
# can be GC'd before the loop schedules it. Mirrors the REST-side
# pattern in ``api/routes/analyses.py`` so MCP and REST kick-offs
# both surface background failures via the same observability path.
# Issue #496 Copilot review (loop 3).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _log_background_task_result(task: asyncio.Task[Any]) -> None:
    """Surface any exception that escaped the analysis background task."""
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "analysis_mcp_background_task_crashed",
            error=str(exc),
            exc_info=exc,
        )


# ============================================================================
# Internal helpers
# ============================================================================


def _missing(field: str) -> list[TextContent]:
    """Standardized response for a required-field-missing error."""
    return _error_response("missing_fields", f"Missing required field: {field}")


def _invalid_uuid(field: str, value: Any) -> list[TextContent]:
    return _error_response(
        "invalid_uuid",
        f"Field '{field}' must be a valid UUID string (got: {value!r}).",
    )


def _parse_uuid(args: dict[str, Any], field: str) -> UUID | list[TextContent]:
    """Pop a UUID-typed field from args, returning either UUID or error response."""
    raw = args.get(field)
    if raw is None or raw == "":
        return _missing(field)
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return _invalid_uuid(field, raw)


async def _verify_context_in_workspace_mcp(
    db: Any,
    *,
    workspace_id: UUID,
    context_id: UUID,
) -> list[TextContent] | None:
    """MCP-side parity of routes/analyses._verify_context_in_workspace.

    Thin wrapper over ``query_service.verify_context_in_workspace`` —
    the SELECT is shared with the REST-side variant so the boundary
    check shape lives in one place. Returns an error response when
    the context does not belong to the workspace; None on pass.
    404-equivalent payload (``context_not_found``) keeps existence
    from leaking.
    """
    from services.analysis import query_service

    if not await query_service.verify_context_in_workspace(
        db, workspace_id=workspace_id, context_id=context_id
    ):
        return _error_response(
            "context_not_found",
            f"Context {context_id} not found or you don't have access to it.",
            context_id=str(context_id),
        )
    return None


def _gate_error_response(exc: Exception) -> list[TextContent]:
    """Map an analysis_gates / orchestrator exception to an MCP error envelope.

    Keeps gate logic identical between REST (raises) and MCP (envelope)
    by converting at exactly one place. The REST handler relies on
    FastAPI's global ``MemoryCloudException`` handler — this function
    is the MCP equivalent.
    """
    if isinstance(exc, (AuthorizationError, NotFoundException)):
        # 403/404 from PermissionService.check_workspace_owner (and other
        # PermissionService methods composed by analysis_gates). The uniform
        # ``"Insufficient permissions"`` / ``"<resource> not found"`` messages
        # come straight from the domain exception; the per-deny-path
        # classification (workspace_deleted / role_too_low / etc.) lives in
        # AuthorizationError.details["reason"] for structured logs, not for
        # MCP clients.
        return _error_response(
            "permission_denied",
            exc.message,
            status_code=exc.status_code,
        )
    if isinstance(exc, HTTPException):
        # Defensive fall-through for any non-PermissionService HTTPException
        # that might propagate from FastAPI deps in this composition.
        return _error_response(
            "permission_denied",
            str(exc.detail) if exc.detail else "Access denied.",
            status_code=exc.status_code,
        )
    if isinstance(exc, FeatureNotAvailableError):
        return _error_response(
            "feature_not_available",
            exc.message,
            feature=exc.details.get("feature", "memory_analysis"),
        )
    if isinstance(exc, QuotaExceededError):
        # Issue #496 Copilot review fix: forward the structured detail
        # fields (used_today, limit_today, addon_bonus, remaining_today,
        # resets_at) so MCP clients can render the same quota dashboard
        # the REST 429 body supports — not just ``quota_type``.
        return _error_response(
            "quota_exceeded",
            exc.message,
            **{k: v for k, v in exc.details.items() if v is not None},
        )
    if isinstance(exc, ValidationError):
        return _error_response(
            "validation_error",
            exc.message,
            **{k: v for k, v in exc.details.items() if v is not None},
        )
    if isinstance(exc, ConflictError):
        # Surface ``run_id`` as a structured field so MCP clients don't
        # have to parse the message text (Issue #496 spec: 409 body
        # carries the existing run_id).
        return _error_response(
            "conflict",
            exc.message,
            **{k: v for k, v in exc.details.items() if v is not None},
        )
    # Unexpected — log + generic error.
    logger.error("analysis_mcp_unexpected_error", error=str(exc), exc_info=True)
    return _error_response("internal_error", str(exc))


def _serialize_run_row(row: Any) -> dict[str, Any]:
    """Project a ``MemoryAnalysis`` row into the MCP response dict.

    Mirrors ``AnalysisRow`` in ``routes/analyses.py`` so REST and MCP
    consumers see the same fields. Datetimes are emitted with explicit
    UTC ``Z`` suffix via ``to_utc_iso`` (#489 wire-format guarantee).
    """
    from utils.datetime import to_utc_iso

    return {
        "run_id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "context_id": str(row.context_id),
        "status": row.status,
        "triggered_by": row.triggered_by,
        "started_at": to_utc_iso(row.started_at),
        "finished_at": to_utc_iso(row.finished_at) if row.finished_at else None,
        "input_count": int(row.input_count) if row.input_count is not None else 0,
        "cost_estimated_cents": (
            int(row.cost_estimated_cents) if row.cost_estimated_cents is not None else None
        ),
        "cost_actual_cents": (
            int(row.cost_actual_cents) if row.cost_actual_cents is not None else None
        ),
        "error": row.error,
        "cancellation_reason": row.cancellation_reason,
    }


# ============================================================================
# analyze_context — POST /analyses (or dry_run preview)
# ============================================================================


async def handle_analyze_context(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Start a new run, or return a cost preview when ``dry_run=true``.

    Shares the full 4-gate chain with REST POST /analyses via
    ``check_memory_analysis_access_mcp(require_quota=True)``.
    """
    from db.base import get_db

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    parsed = _parse_uuid(args, "context_id")
    if isinstance(parsed, list):
        return parsed
    context_id: UUID = parsed

    dry_run = bool(args.get("dry_run", False))

    start_time = time.time()
    async for db in get_db():
        try:
            # Boundary check before gate so a stolen context UUID does not
            # leak gate behavior to the caller.
            err = await _verify_context_in_workspace_mcp(
                db, workspace_id=workspace_id, context_id=context_id
            )
            if err:
                return err

            from auth.analysis_gates import check_memory_analysis_access_mcp

            try:
                await check_memory_analysis_access_mcp(
                    db,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    require_quota=True,
                )
            except Exception as gate_exc:
                return _gate_error_response(gate_exc)

            if dry_run:
                # Preview path — same memory count semantics as REST /preview
                # (delegates to ``query_service.count_context_memories``).
                from services.analysis import query_service
                from services.analysis.preview import DEFAULT_MODEL_ID, estimate_cost

                memory_count = await query_service.count_context_memories(
                    db, workspace_id=workspace_id, context_id=context_id
                )
                estimate = estimate_cost(memory_count, model_id=DEFAULT_MODEL_ID)
                await _log_tool_usage(
                    db,
                    user_id,
                    "analyze_context",
                    start_time,
                    200,
                    context_id=context_id,
                    workspace_id=workspace_id,
                )
                return _success_response(
                    dry_run=True,
                    memory_count=estimate.memory_count,
                    cluster_count_estimate=estimate.cluster_count_estimate,
                    estimated_cost_cents=estimate.estimated_cost_cents,
                    model_id=estimate.model_id,
                    breakdown=estimate.breakdown,
                )

            # Real run — orchestrator.start() + commit + create_task.
            from services.analysis.orchestrator import (
                AnalysisOrchestrator,
                AnalysisParams,
            )
            from tasks.analysis_tasks import run_analysis_task

            params = AnalysisParams(
                from_dt=None,
                to_dt=None,
                types=args.get("types"),
                tags=args.get("tags"),
                min_importance=args.get("min_importance"),
                query=args.get("query"),
                model_id=args.get("model_id"),
                extra={
                    "from": args.get("from"),
                    "to": args.get("to"),
                },
            )
            try:
                analysis = await AnalysisOrchestrator(db).start(
                    workspace_id=workspace_id,
                    context_id=context_id,
                    user_id=user_id,
                    params=params,
                )
            except (ConflictError, ValidationError) as orch_exc:
                await db.rollback()
                return _gate_error_response(orch_exc)

            await db.commit()
            # Strong-ref + done callback so the task is not GC'd before
            # the loop schedules it AND any background exception is
            # logged. Same pattern as ``api/routes/analyses.py``.
            task = asyncio.create_task(run_analysis_task(analysis.id))
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_log_background_task_result)

            await _log_tool_usage(
                db,
                user_id,
                "analyze_context",
                start_time,
                202,
                context_id=context_id,
                workspace_id=workspace_id,
            )
            from utils.datetime import to_utc_iso

            return _success_response(
                run_id=str(analysis.id),
                status=analysis.status,
                started_at=to_utc_iso(analysis.started_at),
            )

        except Exception as e:
            await db.rollback()
            logger.error("analyze_context_failed", error=str(e), exc_info=True)
            await _log_tool_usage(
                db, user_id, "analyze_context", start_time, 500, workspace_id=workspace_id
            )
            return _error_response("analyze_context_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


# ============================================================================
# get_analysis — GET /analyses/{run_id}
# ============================================================================


async def handle_get_analysis(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Fetch one run by id, scoped to workspace."""
    from db.base import get_db

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    parsed = _parse_uuid(args, "run_id")
    if isinstance(parsed, list):
        return parsed
    run_id: UUID = parsed

    start_time = time.time()
    async for db in get_db():
        try:
            from auth.analysis_gates import check_memory_analysis_access_mcp

            try:
                await check_memory_analysis_access_mcp(
                    db,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    require_quota=False,  # Read path — quota gate skipped.
                )
            except Exception as gate_exc:
                return _gate_error_response(gate_exc)

            from services.analysis import query_service

            row = await query_service.get_analysis(db, workspace_id=workspace_id, run_id=run_id)
            if row is None:
                await _log_tool_usage(
                    db, user_id, "get_analysis", start_time, 404, workspace_id=workspace_id
                )
                return _error_response(
                    "run_not_found",
                    f"Analysis run {run_id} not found.",
                    run_id=str(run_id),
                )
            await _log_tool_usage(
                db, user_id, "get_analysis", start_time, 200, workspace_id=workspace_id
            )
            return _success_response(**_serialize_run_row(row))

        except Exception as e:
            logger.error("get_analysis_failed", error=str(e), exc_info=True)
            await _log_tool_usage(
                db, user_id, "get_analysis", start_time, 500, workspace_id=workspace_id
            )
            return _error_response("get_analysis_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


# ============================================================================
# list_analyses — GET /analyses
# ============================================================================


async def handle_list_analyses(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List runs for a context, newest first, with cursor pagination."""
    from db.base import get_db

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    parsed = _parse_uuid(args, "context_id")
    if isinstance(parsed, list):
        return parsed
    context_id: UUID = parsed

    limit = args.get("limit")
    cursor = args.get("cursor")

    start_time = time.time()
    async for db in get_db():
        try:
            err = await _verify_context_in_workspace_mcp(
                db, workspace_id=workspace_id, context_id=context_id
            )
            if err:
                return err

            from auth.analysis_gates import check_memory_analysis_access_mcp

            try:
                await check_memory_analysis_access_mcp(
                    db,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    require_quota=False,
                )
            except Exception as gate_exc:
                return _gate_error_response(gate_exc)

            from services.analysis import query_service

            rows, next_cursor = await query_service.list_analyses(
                db,
                workspace_id=workspace_id,
                context_id=context_id,
                limit=int(limit) if isinstance(limit, int) else None,
                cursor=cursor if isinstance(cursor, str) else None,
            )
            await _log_tool_usage(
                db, user_id, "list_analyses", start_time, 200, workspace_id=workspace_id
            )
            return _success_response(
                items=[_serialize_run_row(row) for row in rows],
                next_cursor=next_cursor,
            )

        except Exception as e:
            logger.error("list_analyses_failed", error=str(e), exc_info=True)
            await _log_tool_usage(
                db, user_id, "list_analyses", start_time, 500, workspace_id=workspace_id
            )
            return _error_response("list_analyses_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


# ============================================================================
# get_active_analysis — GET /analyses/active
# ============================================================================


async def handle_get_active_analysis(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Return the most recent succeeded run for a context (or 404)."""
    from db.base import get_db

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    parsed = _parse_uuid(args, "context_id")
    if isinstance(parsed, list):
        return parsed
    context_id: UUID = parsed

    start_time = time.time()
    async for db in get_db():
        try:
            err = await _verify_context_in_workspace_mcp(
                db, workspace_id=workspace_id, context_id=context_id
            )
            if err:
                return err

            from auth.analysis_gates import check_memory_analysis_access_mcp

            try:
                await check_memory_analysis_access_mcp(
                    db,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    require_quota=False,
                )
            except Exception as gate_exc:
                return _gate_error_response(gate_exc)

            from services.analysis import query_service

            row = await query_service.get_active_analysis(
                db, workspace_id=workspace_id, context_id=context_id
            )
            if row is None:
                await _log_tool_usage(
                    db,
                    user_id,
                    "get_active_analysis",
                    start_time,
                    404,
                    workspace_id=workspace_id,
                )
                return _error_response(
                    "no_succeeded_run",
                    f"No succeeded analysis run found for context {context_id}.",
                    context_id=str(context_id),
                )
            await _log_tool_usage(
                db,
                user_id,
                "get_active_analysis",
                start_time,
                200,
                workspace_id=workspace_id,
            )
            return _success_response(**_serialize_run_row(row))

        except Exception as e:
            logger.error("get_active_analysis_failed", error=str(e), exc_info=True)
            await _log_tool_usage(
                db,
                user_id,
                "get_active_analysis",
                start_time,
                500,
                workspace_id=workspace_id,
            )
            return _error_response("get_active_analysis_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


# ============================================================================
# get_cluster — drill-down with paginated memories
# ============================================================================


async def handle_get_cluster(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Return cluster metadata + paginated member memories (Layer 1+2)."""
    from db.base import get_db

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    parsed_run = _parse_uuid(args, "run_id")
    if isinstance(parsed_run, list):
        return parsed_run
    run_id: UUID = parsed_run

    cluster_index_raw = args.get("cluster_index")
    if cluster_index_raw is None:
        return _missing("cluster_index")
    try:
        cluster_index = int(cluster_index_raw)
    except (TypeError, ValueError):
        return _error_response(
            "invalid_cluster_index",
            f"cluster_index must be a non-negative integer (got: {cluster_index_raw!r}).",
        )
    if cluster_index < 0:
        return _error_response(
            "invalid_cluster_index",
            "cluster_index must be a non-negative integer.",
        )

    limit = args.get("limit")
    cursor = args.get("cursor")

    start_time = time.time()
    async for db in get_db():
        try:
            from auth.analysis_gates import check_memory_analysis_access_mcp

            try:
                await check_memory_analysis_access_mcp(
                    db,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    require_quota=False,
                )
            except Exception as gate_exc:
                return _gate_error_response(gate_exc)

            from services.analysis import query_service

            cluster = await query_service.get_cluster(
                db,
                workspace_id=workspace_id,
                run_id=run_id,
                cluster_index=cluster_index,
                limit=int(limit) if isinstance(limit, int) else None,
                cursor=cursor if isinstance(cursor, str) else None,
            )
            if cluster is None:
                await _log_tool_usage(
                    db, user_id, "get_cluster", start_time, 404, workspace_id=workspace_id
                )
                return _error_response(
                    "cluster_not_found",
                    (
                        f"Cluster #{cluster_index} not found in run {run_id} "
                        "(or run not in your workspace)."
                    ),
                    run_id=str(run_id),
                    cluster_index=cluster_index,
                )
            await _log_tool_usage(
                db, user_id, "get_cluster", start_time, 200, workspace_id=workspace_id
            )
            return _success_response(**cluster)

        except Exception as e:
            logger.error("get_cluster_failed", error=str(e), exc_info=True)
            await _log_tool_usage(
                db, user_id, "get_cluster", start_time, 500, workspace_id=workspace_id
            )
            return _error_response("get_cluster_error", str(e))

    return _error_response("internal_error", "Database session unavailable")
