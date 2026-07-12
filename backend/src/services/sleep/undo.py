"""Per-merge undo for judged dedup merges (#1209).

``rollback_sleep_run`` reverses a WHOLE run; the user-facing correction loop
needs "restore just this memory". This module owns the shared restore
machinery — used by the admin REST endpoint (`/admin/sleep/actions/...`) and
by the MCP run-level rollback (which re-embeds through the same helper), so
the two paths cannot drift.

Reversibility is bounded by the retention policy (#1209): once
``sleep_merge_retention_days`` purges a merge loser, the undo target is gone
and the error says so explicitly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from models.sleep import SleepAction, SleepReport
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


async def revert_shadow_merge_edge(
    db: AsyncSession,
    *,
    user_id: str,
    winner_id: UUID,
    loser_id: UUID,
    prior_edge: dict[str, Any] | None,
) -> bool:
    """Undo ONE shadow-mode merge's supersedes edge (#1208).

    Shared by run-level rollback and per-merge undo so the two paths cannot
    drift. The shadow merge upserted ``supersedes`` over whatever edge
    existed between (winner, loser); undoing it means:

    - ``prior_edge`` snapshot present → restore the pre-merge edge state
      (type/origin/weight/confidence/metadata) with a direct UPDATE — an
      upsert cannot do this because the sticky-origin CASE refuses to
      downgrade a non-hebbian origin back to hebbian.
    - no snapshot (the merge CREATED the edge) → delete it, but only after
      verifying it still is a ``supersedes`` edge: ``unique_edge`` is keyed
      on (user, src, dst) regardless of edge_type, so a blind delete could
      remove an unrelated association written since.

    Returns:
        True if an edge was restored or deleted; False if nothing matched
        (the shadow merge was already undone, or the edge was retyped by a
        later writer).
    """
    from sqlalchemy import delete as sa_delete

    from models.memory import EDGE_TYPE_SUPERSEDES, NeuralMemoryEdge

    if prior_edge:
        result = await db.execute(
            sa_update(NeuralMemoryEdge)
            .where(
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.src_id == winner_id,
                NeuralMemoryEdge.dst_id == loser_id,
                NeuralMemoryEdge.edge_type == EDGE_TYPE_SUPERSEDES,
            )
            .values(
                edge_type=prior_edge.get("edge_type") or "neural_association",
                origin=prior_edge.get("origin") or "hebbian",
                weight=prior_edge.get("weight", 0.0),
                confidence=prior_edge.get("confidence", 1.0),
                edge_metadata=prior_edge.get("edge_metadata"),
                last_updated=utcnow(),
            )
        )
    else:
        result = await db.execute(
            sa_delete(NeuralMemoryEdge).where(
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.src_id == winner_id,
                NeuralMemoryEdge.dst_id == loser_id,
                NeuralMemoryEdge.edge_type == EDGE_TYPE_SUPERSEDES,
            )
        )
    return (result.rowcount or 0) > 0


class UndoMergeError(Exception):
    """A per-merge undo could not be performed.

    Attributes:
        code: Stable machine-readable reason (``action_not_found`` |
            ``not_a_merge`` | ``memory_purged`` | ``already_restored`` |
            ``not_merge_deleted``).
        message: Human-readable explanation.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def re_embed_memory_to_qdrant(
    memory: Any,
    user_id: str,
    embedding_svc: Any,
    workspace_id: str,
    context_id: str,
    collection_name: str,
) -> None:
    """Re-embed a restored memory back into Qdrant.

    Merge hard-deletes the loser's vector (orphan-vector prevention), so any
    restore path must rebuild it. Shared by run-level rollback and per-merge
    undo.
    """
    from db.qdrant import add_memory_to_qdrant

    vector = await embedding_svc.embed(
        memory.summary or "",
        user_id=user_id,
        context_id=context_id,
        workspace_id=workspace_id,
    )
    payload = {
        "user_id": user_id,
        "summary": memory.summary,
        "context_summary": memory.context_summary or "",
        "type": memory.type,
        "importance": memory.importance,
        "scope": memory.scope,
        "tags": memory.tags or [],
        "created_at": (memory.created_at.isoformat() + "Z" if memory.created_at else None),
        "updated_at": (memory.updated_at.isoformat() + "Z" if memory.updated_at else None),
    }
    await add_memory_to_qdrant(
        user_id=user_id,
        memory_id=memory.id,
        vector=vector,
        payload=payload,
        workspace_id=workspace_id,
        context_id=context_id,
        collection_name=collection_name,
    )


async def _resolve_embedding_info(
    db: AsyncSession, context_id: UUID | None
) -> tuple[str | None, str]:
    """Context's embedding model + Qdrant collection (defaults when row-less)."""
    from db.qdrant import get_collection_name
    from models.config import ContextSearchConfig

    collection_name = "kagura_memories"
    embedding_model: str | None = None
    if context_id:
        result = await db.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
        )
        search_config = result.scalar_one_or_none()
        if search_config and search_config.embedding_model:
            collection_name = get_collection_name(
                search_config.embedding_model,
                search_config.embedding_dimensions,
            )
            embedding_model = search_config.embedding_model
    return embedding_model, collection_name


async def undo_merge_action(
    db: AsyncSession,
    action_id: int,
    *,
    acting_user_id: str,
) -> dict[str, Any]:
    """Restore the loser of ONE dedup merge (row + vector) and audit the undo.

    Self-scoped: the merge's report must belong to ``acting_user_id`` (the
    same Phase-1 scoping the admin sleep trigger uses).

    Returns:
        Summary dict: restored memory id, winner id, report id, undo action id.

    Raises:
        UndoMergeError: with a stable ``code`` for every non-restorable state
            — including ``memory_purged`` once the retention policy
            (``sleep_merge_retention_days``) has hard-deleted the loser.
    """
    from services.embedding_service import EmbeddingService

    result = await db.execute(
        select(SleepAction, SleepReport)
        .join(SleepReport, SleepAction.report_id == SleepReport.id)
        .where(SleepAction.id == action_id, SleepReport.user_id == acting_user_id)
    )
    row = result.first()
    if row is None:
        raise UndoMergeError(
            "action_not_found",
            f"Sleep action {action_id} not found or not owned by you.",
        )
    action, report = row

    if action.action_type != "merge" or action.phase != "dedup_merge":
        raise UndoMergeError(
            "not_a_merge",
            f"Sleep action {action_id} is '{action.phase}/{action.action_type}', "
            "not a dedup merge.",
        )

    loser_id = action.target_id
    winner_id = action.memory_id
    if loser_id is None:
        raise UndoMergeError("not_a_merge", f"Sleep action {action_id} records no merge loser.")

    # #1208 shadow-mode merge: the loser was never deleted, so the restore
    # path below (deleted_at checks + re-embed) does not apply — undo means
    # reverting the supersedes edge (winner → loser) instead.
    if (action.details or {}).get("mode") == "shadow":
        if winner_id is None:
            raise UndoMergeError(
                "not_a_merge", f"Sleep action {action_id} records no shadow-merge winner."
            )
        reverted = await revert_shadow_merge_edge(
            db,
            user_id=report.user_id,
            winner_id=winner_id,
            loser_id=loser_id,
            prior_edge=(action.details or {}).get("prior_edge"),
        )
        if not reverted:
            raise UndoMergeError(
                "already_restored",
                f"No supersedes edge {winner_id} → {loser_id} exists — this shadow "
                "merge was already undone, or the edge was since retyped.",
            )
        undo_action = SleepAction(
            report_id=action.report_id,
            phase="dedup_merge",
            action_type="undo_merge",
            memory_id=loser_id,
            target_id=winner_id,
            details={
                "undone_action_id": action.id,
                "undone_by": acting_user_id,
                "mode": "shadow",
            },
        )
        db.add(undo_action)
        await db.commit()
        logger.info(
            "dedup_shadow_merge_undone",
            action_id=action_id,
            unshadowed_memory_id=str(loser_id),
            winner_id=str(winner_id),
            report_id=str(action.report_id),
        )
        return {
            "restored_memory_id": str(loser_id),
            "winner_id": str(winner_id),
            "report_id": str(action.report_id),
            "undone_action_id": action.id,
        }

    mem_result = await db.execute(select(Memory).where(Memory.id == loser_id))
    loser = mem_result.scalar_one_or_none()
    if loser is None:
        raise UndoMergeError(
            "memory_purged",
            f"Merged memory {loser_id} no longer exists — it was hard-deleted by the "
            "merge retention policy (sleep_merge_retention_days), which bounds how "
            "long merges stay reversible.",
        )
    if loser.deleted_at is None:
        raise UndoMergeError(
            "already_restored",
            f"Memory {loser_id} is not deleted — this merge was already undone or rolled back.",
        )
    if loser.deleted_by != "sleep_maintenance":
        raise UndoMergeError(
            "not_merge_deleted",
            f"Memory {loser_id} was deleted by '{loser.deleted_by}', not by sleep "
            "maintenance — undo-merge refuses to override a different deletion.",
        )

    embedding_model, collection_name = await _resolve_embedding_info(db, report.context_id)

    await db.execute(
        sa_update(Memory).where(Memory.id == loser_id).values(deleted_at=None, deleted_by=None)
    )

    # Resolve workspace_id — required by add_memory_to_qdrant. Mirrors the
    # run-level rollback's Context fallback: workspace-less reports exist,
    # and the shared-helper promise is that the two restore paths never
    # drift (a bare "" would hard-raise inside the Qdrant layer).
    ws_id = str(report.workspace_id) if report.workspace_id else ""
    if not ws_id and report.context_id:
        from models.auth import Context

        ctx_ws = (
            await db.execute(select(Context.workspace_id).where(Context.id == report.context_id))
        ).scalar_one_or_none()
        if ctx_ws:
            ws_id = str(ctx_ws)
    ctx_id = str(report.context_id) if report.context_id else ""
    embedding_svc = EmbeddingService(db, model=embedding_model)
    await re_embed_memory_to_qdrant(
        loser,
        report.user_id,
        embedding_svc,
        ws_id,
        ctx_id,
        collection_name,
    )

    # Audit symmetry: the undo is a SleepAction on the SAME report, so the
    # merge's full history (merge -> undo_merge) reads out of one audit log.
    undo_action = SleepAction(
        report_id=action.report_id,
        phase="dedup_merge",
        action_type="undo_merge",
        memory_id=loser_id,
        target_id=winner_id,
        details={
            "undone_action_id": action.id,
            "undone_by": acting_user_id,
        },
    )
    db.add(undo_action)
    await db.commit()

    logger.info(
        "dedup_merge_undone",
        action_id=action_id,
        restored_memory_id=str(loser_id),
        winner_id=str(winner_id) if winner_id else None,
        report_id=str(action.report_id),
    )
    return {
        "restored_memory_id": str(loser_id),
        "winner_id": str(winner_id) if winner_id else None,
        "report_id": str(action.report_id),
        "undone_action_id": action.id,
    }
