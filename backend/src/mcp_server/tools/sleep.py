"""MCP tool handlers: Sleep Maintenance observability + rollback.

Issue #164: get_sleep_history, get_sleep_report, rollback_sleep_run.
"""

import time
from typing import Any, NamedTuple, cast
from uuid import UUID

from mcp.types import TextContent
from sqlalchemy import select
from sqlalchemy.engine import CursorResult

from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _ContextNotFoundError,
    _error_response,
    _log_tool_usage,
    _resolve_context,
    _resolve_context_id,
    _success_response,
)
from utils.logger import get_logger

# #1440: bind through ``utils.logger.get_logger`` (structlog) like the rest of
# the project. This module previously used stdlib ``logging.getLogger``, whose
# ``Logger._log()`` rejects unknown kwargs — so the structlog-style
# ``logger.warning("event", key=value)`` call in the shadow-merge rollback path
# raised TypeError and was swallowed into ``rollback_summary["errors"]``,
# turning an edge mismatch into a reported ``partial_rollback``. Identical
# bug class to the one fixed in ``api/routes/auth.py`` (PR #522).
#
# #1450 follow-up: that mismatch was never one situation. Only "already undone"
# is benign; "a later writer changed or removed the edge" means the action was
# not reversed at all, and #1440's fix made THAT case silent. The helper now
# returns a tri-state and the two are handled apart, below.
logger = get_logger(__name__)


def _report_to_summary(report: Any) -> dict[str, Any]:
    """Convert SleepReport to summary dict for history listing."""
    return {
        "report_id": str(report.id),
        "context_id": str(report.context_id) if report.context_id else None,
        "status": report.status,
        "started_at": report.started_at.isoformat() if report.started_at else None,
        "completed_at": report.completed_at.isoformat() if report.completed_at else None,
        "memories_processed": report.memories_processed,
        "edges_created": report.edges_created,
        "memories_merged": report.memories_merged,
        "memories_promoted": report.memories_promoted,
        "llm_calls_made": report.llm_calls_made,
        "llm_tokens_used": report.llm_tokens_used,
        # #1183: magnitude behind a 'degraded'/'failed' grading.
        "llm_call_failures": report.llm_call_failures,
    }


def _report_to_detail(report: Any) -> dict[str, Any]:
    """Convert SleepReport to full detail dict."""
    summary = _report_to_summary(report)
    summary.update(
        {
            "memories_flagged": report.memories_flagged,
            "embedding_calls_made": report.embedding_calls_made,
            "error_message": report.error_message,
            "edge_discovery_result": report.edge_discovery_result,
            "dedup_result": report.dedup_result,
            "importance_result": report.importance_result,
            "consolidation_result": report.consolidation_result,
            "reindex_result": report.reindex_result,
            "merge_retention_result": report.merge_retention_result,
        }
    )
    return summary


def _action_to_dict(action: Any) -> dict[str, Any]:
    """Convert SleepAction to dict."""
    return {
        "id": str(action.id),
        "phase": action.phase,
        "action_type": action.action_type,
        "memory_id": str(action.memory_id) if action.memory_id else None,
        "target_id": str(action.target_id) if action.target_id else None,
        "details": action.details,
        "created_at": action.created_at.isoformat() if action.created_at else None,
    }


def _validate_report_id(
    args: dict[str, Any],
) -> tuple[UUID | None, list[TextContent] | None]:
    """Parse and validate report_id from args."""
    report_id_str = args.get("report_id")
    if not report_id_str:
        return None, _error_response("missing_required_fields", "report_id is required.")
    try:
        return UUID(str(report_id_str)), None
    except ValueError:
        return None, _error_response("invalid_report_id", "report_id must be a valid UUID.")


async def _re_embed_to_qdrant(
    memory: Any,
    user_id: str,
    embedding_svc: Any,
    workspace_id: str,
    context_id: str,
    collection_name: str,
) -> None:
    """Re-embed a restored memory back into Qdrant.

    #1209: thin wrapper over the shared restore helper in
    ``services.sleep.undo`` so run-level rollback and per-merge undo cannot
    drift in how they rebuild the vector.
    """
    from services.sleep.undo import re_embed_memory_to_qdrant

    await re_embed_memory_to_qdrant(
        memory,
        user_id,
        embedding_svc,
        workspace_id,
        context_id,
        collection_name,
    )


async def handle_get_sleep_history(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List recent sleep maintenance runs for a context."""
    from db.base import get_db
    from models.sleep import SleepReport

    raw_limit = args.get("limit", 10)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return _error_response(
            "validation_error", "Invalid 'limit': expected an integer between 1 and 50."
        )
    limit = max(1, min(limit, 50))

    start_time = time.time()
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            await _resolve_context(db, user_id, current_context_id)

            stmt = (
                select(SleepReport)
                .where(
                    SleepReport.user_id == user_id,
                    SleepReport.context_id == current_context_id,
                )
                .order_by(SleepReport.started_at.desc())
                .limit(limit)
            )
            result = await db.execute(stmt)
            reports = list(result.scalars().all())

            await _log_tool_usage(
                db,
                user_id,
                "get_sleep_history",
                start_time,
                200,
                current_context_id,
                workspace_id,
            )
            await db.commit()

            return _success_response(
                reports=[_report_to_summary(r) for r in reports],
                count=len(reports),
            )
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            raise

    return _error_response("db_error", "Database unavailable.")


async def handle_get_sleep_report(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Get detailed sleep report with all actions."""
    from db.base import get_db
    from models.sleep import SleepAction, SleepReport

    report_uuid, error = _validate_report_id(args)
    if error or report_uuid is None:
        return error or _error_response("invalid_report_id", "Invalid report_id")

    start_time = time.time()
    async for db in get_db():
        try:
            stmt = select(SleepReport).where(
                SleepReport.id == report_uuid,
                SleepReport.user_id == user_id,
            )
            result = await db.execute(stmt)
            report = result.scalar_one_or_none()

            if not report:
                return _error_response(
                    "report_not_found",
                    f"Sleep report {report_uuid} not found or not owned by you.",
                )

            actions_stmt = (
                select(SleepAction)
                .where(SleepAction.report_id == report_uuid)
                .order_by(SleepAction.id)
            )
            actions_result = await db.execute(actions_stmt)
            actions = list(actions_result.scalars().all())

            # Resolve context_id from report for log_tool_usage
            ctx_id = report.context_id

            await _log_tool_usage(
                db,
                user_id,
                "get_sleep_report",
                start_time,
                200,
                ctx_id,
                workspace_id,
            )
            await db.commit()

            return _success_response(
                report=_report_to_detail(report),
                actions=[_action_to_dict(a) for a in actions],
                action_count=len(actions),
            )
        except Exception:
            await db.rollback()
            raise

    return _error_response("db_error", "Database unavailable.")


class _RollbackCtx(NamedTuple):
    """Per-run values every action handler needs (#1456).

    Bundled so the handlers take ``(db, action, ctx, summary)`` instead of eight
    positional arguments each. Everything here is resolved once, before the
    dispatch loop.
    """

    user_id: str
    ws_id: str
    ctx_id_str: str
    collection_name: str
    embedding_svc: Any
    # Rows pre-fetched for re-embedding, keyed by id. A miss means the row is
    # gone (retention purge) — the merge handler treats that as an error rather
    # than reporting a restore that never happened.
    memory_cache: dict[UUID, Any]
    edge_repo: Any


async def _undo_create_edge(
    db: Any, action: Any, ctx: _RollbackCtx, summary: dict[str, Any]
) -> None:
    """Delete an edge the run created."""
    if not (action.memory_id and action.target_id):
        return
    await ctx.edge_repo.delete_edge(
        user_id=ctx.user_id,
        src_id=action.memory_id,
        dst_id=action.target_id,
        workspace_id=ctx.ws_id or None,
        context_id=ctx.ctx_id_str or None,
    )
    summary["edges_deleted"] += 1


async def _undo_shadow_merge(
    db: Any, action: Any, ctx: _RollbackCtx, summary: dict[str, Any]
) -> None:
    """Revert a #1208 shadow merge's supersedes edge.

    The loser was never deleted, so undoing the merge means reverting the edge
    (winner=memory_id → loser=target_id): restore the pre-merge edge from the
    ``prior_edge`` snapshot, or verified-delete it when the merge created the
    edge. Shared helper with per-merge undo so the two paths cannot drift.
    """
    from services.sleep.undo import ShadowEdgeRevert, revert_shadow_merge_edge

    if not action.memory_id:
        return

    reverted = await revert_shadow_merge_edge(
        db,
        user_id=ctx.user_id,
        winner_id=action.memory_id,
        loser_id=action.target_id,
        prior_edge=(action.details or {}).get("prior_edge"),
    )
    if reverted is ShadowEdgeRevert.RESTORED:
        summary["merges_reversed"] += 1
    elif reverted is ShadowEdgeRevert.ALREADY_UNDONE:
        # Benign: the edge is already in its post-undo state. Nothing to
        # reverse, nothing to report (#1441).
        logger.warning(
            "shadow_merge_rollback_already_undone",
            src_id=str(action.memory_id),
            dst_id=str(action.target_id),
        )
    else:
        # #1450: a later writer changed or removed the edge, so this action was
        # NOT reversed. Both zero-row causes used to land in the branch above,
        # which made an unreversed merge indistinguishable from a clean rollback
        # outside of a stdout warning. Same treatment as the hard-merge sibling
        # below (a loser that could not be restored): an error, so the run
        # reports partial_rollback rather than success.
        summary["merges_unreversible"] += 1
        summary["errors"].append(
            f"shadow merge {action.memory_id} → {action.target_id} not reversed — "
            "the edge was changed or removed by a later writer, so the pre-merge "
            "state could not be restored"
        )
        logger.warning(
            "shadow_merge_rollback_blocked_by_newer_state",
            src_id=str(action.memory_id),
            dst_id=str(action.target_id),
        )


async def _undo_merge(db: Any, action: Any, ctx: _RollbackCtx, summary: dict[str, Any]) -> None:
    """Restore a merge loser (row + vector), or revert a shadow merge's edge."""
    from sqlalchemy import update as sa_update

    from models.memory import Memory

    if not action.target_id:
        return
    if (action.details or {}).get("mode") == "shadow":
        await _undo_shadow_merge(db, action, ctx, summary)
        return

    restore_result = await db.execute(
        sa_update(Memory)
        .where(Memory.id == action.target_id, Memory.user_id == ctx.user_id)
        .values(deleted_at=None, deleted_by=None)
    )
    loser = ctx.memory_cache.get(action.target_id)
    # #1209: a loser hard-deleted by the merge retention window matches 0 rows —
    # record it as an error instead of a phantom "reversed" (the per-merge undo
    # path returns 409 for the same state; run-level rollback must not report a
    # restore that never happened). Result[Any] at type level, CursorResult at
    # runtime — .rowcount lives on the latter (#1442).
    restore_rows = cast(CursorResult[Any], restore_result).rowcount
    if restore_rows == 0 or loser is None:
        summary["errors"].append(
            f"merge loser {action.target_id} not restorable — purged by the "
            "retention policy (sleep_merge_retention_days)"
        )
        return

    await _re_embed_to_qdrant(
        loser, ctx.user_id, ctx.embedding_svc, ctx.ws_id, ctx.ctx_id_str, ctx.collection_name
    )
    summary["merges_reversed"] += 1


async def _undo_update_importance(
    db: Any, action: Any, ctx: _RollbackCtx, summary: dict[str, Any]
) -> None:
    """Restore the pre-run importance, best-effort syncing the Qdrant payload."""
    from sqlalchemy import update as sa_update

    from db.qdrant import update_memory_payload_in_qdrant
    from models.memory import Memory
    from utils.datetime import utcnow

    old_importance = (action.details or {}).get("old_importance")
    if not (action.memory_id and old_importance is not None):
        return

    await db.execute(
        sa_update(Memory)
        .where(Memory.id == action.memory_id, Memory.user_id == ctx.user_id)
        .values(importance=old_importance, updated_at=utcnow())
    )
    try:
        await update_memory_payload_in_qdrant(
            memory_id=action.memory_id,
            payload_updates={"importance": old_importance},
            collection_name=ctx.collection_name,
        )
    except Exception:
        pass  # Non-fatal: Qdrant payload sync
    summary["importance_restored"] += 1


async def _undo_promote(db: Any, action: Any, ctx: _RollbackCtx, summary: dict[str, Any]) -> None:
    """Send a promoted memory back to the working scope."""
    from sqlalchemy import update as sa_update

    from models.memory import Memory
    from utils.datetime import utcnow

    if not action.memory_id:
        return
    await db.execute(
        sa_update(Memory)
        .where(Memory.id == action.memory_id, Memory.user_id == ctx.user_id)
        .values(scope="working", promoted_at=None, updated_at=utcnow())
    )
    summary["promotions_reversed"] += 1


async def _undo_archive(db: Any, action: Any, ctx: _RollbackCtx, summary: dict[str, Any]) -> None:
    """Un-delete an archived memory and rebuild its vector."""
    from sqlalchemy import update as sa_update

    from models.memory import Memory

    if not action.memory_id:
        return
    await db.execute(
        sa_update(Memory)
        .where(Memory.id == action.memory_id, Memory.user_id == ctx.user_id)
        .values(deleted_at=None, deleted_by=None)
    )
    mem = ctx.memory_cache.get(action.memory_id)
    if mem:
        await _re_embed_to_qdrant(
            mem, ctx.user_id, ctx.embedding_svc, ctx.ws_id, ctx.ctx_id_str, ctx.collection_name
        )
    summary["archives_restored"] += 1


# Action type → undoer. An action type absent from this table is skipped
# silently, which is the pre-existing behaviour of the if/elif chain this
# replaces — ``undo_merge`` rows (written by the per-merge undo onto the same
# report) rely on it.
_UNDO_HANDLERS = {
    "create_edge": _undo_create_edge,
    "merge": _undo_merge,
    "update_importance": _undo_update_importance,
    "promote": _undo_promote,
    "archive": _undo_archive,
}


async def _resolve_rollback_workspace_id(db: Any, report: Any) -> str:
    """Report's workspace id, falling back to its context's (#1456 extract)."""
    if report.workspace_id:
        return str(report.workspace_id)
    if not report.context_id:
        return ""
    from models.auth import Context

    ctx_result = await db.execute(
        select(Context.workspace_id).where(Context.id == report.context_id)
    )
    ctx_ws = ctx_result.scalar_one_or_none()
    return str(ctx_ws) if ctx_ws else ""


async def _prefetch_rollback_memories(db: Any, actions: list[Any]) -> dict[UUID, Any]:
    """Rows the rollback will re-embed, fetched in one query.

    #1208: shadow-mode merges never deleted the loser's vector, so they need no
    re-embed — their undo is reverting the edge.
    """
    from models.memory import Memory

    re_embed_ids: set[UUID] = set()
    for a in actions:
        if a.action_type == "merge" and a.target_id and (a.details or {}).get("mode") != "shadow":
            re_embed_ids.add(a.target_id)
        elif a.action_type == "archive" and a.memory_id:
            re_embed_ids.add(a.memory_id)
    if not re_embed_ids:
        return {}
    mem_result = await db.execute(select(Memory).where(Memory.id.in_(list(re_embed_ids))))
    return {m.id: m for m in mem_result.scalars().all()}


async def handle_rollback_sleep_run(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Rollback all actions from a completed sleep maintenance run."""
    from db.base import get_db
    from db.qdrant import get_collection_name
    from models.config import ContextSearchConfig
    from models.sleep import SleepAction, SleepReport
    from repositories.neural_edge import NeuralEdgeRepository
    from services.embedding_service import EmbeddingService
    from utils.datetime import utcnow

    report_uuid, error = _validate_report_id(args)
    if error or report_uuid is None:
        return error or _error_response("invalid_report_id", "Invalid report_id")

    start_time = time.time()
    async for db in get_db():
        try:
            stmt = select(SleepReport).where(
                SleepReport.id == report_uuid,
                SleepReport.user_id == user_id,
            )
            result = await db.execute(stmt)
            report = result.scalar_one_or_none()

            if not report:
                return _error_response(
                    "report_not_found",
                    f"Sleep report {report_uuid} not found or not owned by you.",
                )

            # Viewers cannot rollback
            perm_error = await _check_viewer_permission(
                db, user_id, workspace_id, "rollback sleep runs"
            )
            if perm_error:
                return perm_error

            # #1183: 'degraded' runs (partial judge failures) still executed
            # real merges/promotions — they must stay rollbackable.
            if report.status not in ("completed", "degraded"):
                return _error_response(
                    "invalid_status",
                    "Can only rollback 'completed' or 'degraded' reports. "
                    f"Current status: '{report.status}'.",
                )

            # Fetch actions in reverse order
            actions_stmt = (
                select(SleepAction)
                .where(SleepAction.report_id == report_uuid)
                .order_by(SleepAction.id.desc())
            )
            actions_result = await db.execute(actions_stmt)
            actions = list(actions_result.scalars().all())

            if not actions:
                return _error_response(
                    "no_actions",
                    "No recorded actions to rollback. This report may pre-date action recording.",
                )

            # Get context embedding info for re-embedding
            collection_name = "kagura_memories"
            embedding_model = None
            if report.context_id:
                config_stmt = select(ContextSearchConfig).where(
                    ContextSearchConfig.context_id == report.context_id
                )
                config_result = await db.execute(config_stmt)
                search_config = config_result.scalar_one_or_none()
                if search_config and search_config.embedding_model:
                    collection_name = get_collection_name(
                        search_config.embedding_model,
                        search_config.embedding_dimensions,
                    )
                    embedding_model = search_config.embedding_model

            rollback_summary = {
                "edges_deleted": 0,
                "merges_reversed": 0,
                # #1450: recorded merges the rollback could NOT reverse because a
                # later writer changed the edge. Counted separately from
                # ``merges_reversed`` so "0 reversed" stops being the only hint
                # that something was left standing.
                "merges_unreversible": 0,
                "importance_restored": 0,
                "promotions_reversed": 0,
                "archives_restored": 0,
                "errors": [],
            }

            ctx = _RollbackCtx(
                user_id=user_id,
                ws_id=await _resolve_rollback_workspace_id(db, report),
                ctx_id_str=str(report.context_id) if report.context_id else "",
                collection_name=collection_name,
                embedding_svc=EmbeddingService(db, model=embedding_model),
                memory_cache=await _prefetch_rollback_memories(db, actions),
                edge_repo=NeuralEdgeRepository(db),
            )

            for action in actions:
                handler = _UNDO_HANDLERS.get(action.action_type)
                if handler is None:
                    continue
                try:
                    await handler(db, action, ctx, rollback_summary)
                except Exception as e:
                    # Per-action isolation: one failed undo must not abandon the
                    # rest of the run. The error lands in the summary, which is
                    # what flips the report to partial_rollback below.
                    logger.warning(
                        f"rollback_action_failed: action={action.id} "
                        f"type={action.action_type} error={e}"
                    )
                    rollback_summary["errors"].append(
                        f"Action {action.id} ({action.action_type}): {e}"
                    )

            has_errors = bool(rollback_summary["errors"])
            report.status = "rolled_back" if not has_errors else "failed"
            report.completed_at = utcnow()
            if has_errors:
                report.error_message = (
                    f"Partial rollback: {len(rollback_summary['errors'])} action(s) failed"
                )

            ctx_id = report.context_id
            await _log_tool_usage(
                db,
                user_id,
                "rollback_sleep_run",
                start_time,
                200 if not has_errors else 500,
                ctx_id,
                workspace_id,
            )
            await db.commit()

            if has_errors:
                return _error_response(
                    "partial_rollback",
                    f"Rollback completed with {len(rollback_summary['errors'])} error(s). "
                    "Report marked as 'failed' — inspect errors and retry if needed.",
                    report_id=str(report_uuid),
                    rollback_summary=rollback_summary,
                )

            return _success_response(
                report_id=str(report_uuid),
                status="rolled_back",
                rollback_summary=rollback_summary,
            )
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            raise

    return _error_response("db_error", "Database unavailable.")
