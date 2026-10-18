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

from enum import StrEnum
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import TOMBSTONE_PURGER_CLAUSE
from db.point_writer_lock import hold_point_writer_lock
from models.memory import DELETED_BY_SLEEP_MERGE, Memory
from models.sleep import SleepAction, SleepReport
from services.sleep.merge_retention import restore_sleep_tombstone_stmt
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


class ShadowEdgeRevert(StrEnum):
    """Outcome of undoing one shadow merge's supersedes edge (#1450).

    This was a ``bool`` until #1450. ``False`` meant two materially different
    things — "there was nothing left to do" and "a recorded merge action could
    NOT be reversed because newer state is in the way" — and both callers
    collapsed them, so a rollback that left a merge standing still reported
    success. The three states are kept apart here so neither caller can
    re-conflate them.
    """

    RESTORED = "restored"
    """The pre-merge edge was restored, or the merge-created edge was deleted."""

    ALREADY_UNDONE = "already_undone"
    """Nothing to do: the edge is already in its post-undo state. Benign."""

    BLOCKED_BY_NEWER_STATE = "blocked_by_newer_state"
    """A later writer changed or removed the edge, so the pre-merge state could
    not be restored without discarding that newer state. NOT benign — the
    recorded action was not reversed.

    Note this does NOT mean a ``supersedes`` edge is still shadowing the loser:
    reaching this state means the guarded statement found none. What is left
    un-reversed is the edge state the merge overwrote (or, without a snapshot,
    an edge the merge created that a later writer has since repurposed and that
    this undo must not delete)."""


async def revert_shadow_merge_edge(
    db: AsyncSession,
    *,
    user_id: str,
    winner_id: UUID,
    loser_id: UUID,
    prior_edge: dict[str, Any] | None,
) -> ShadowEdgeRevert:
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

    When the guarded statement matches no row, the current edge is read back to
    tell the two zero-row causes apart (#1450). ``unique_edge`` is keyed on
    (user, src, dst), so there is at most one row to inspect.

    Returns:
        A :class:`ShadowEdgeRevert`. Callers MUST treat
        ``BLOCKED_BY_NEWER_STATE`` as a failure to reverse — the recorded action
        could not be undone without discarding newer edge state.
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
    # DML through AsyncSession.execute() is typed Result[Any] but is a
    # CursorResult at runtime, which is what carries .rowcount (#1442).
    if (cast(CursorResult[Any], result).rowcount or 0) > 0:
        return ShadowEdgeRevert.RESTORED

    current_type = (
        await db.execute(
            select(NeuralMemoryEdge.edge_type).where(
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.src_id == winner_id,
                NeuralMemoryEdge.dst_id == loser_id,
            )
        )
    ).scalar_one_or_none()

    if prior_edge:
        # The undo target is the snapshot's type. Finding exactly that means a
        # previous undo already landed; anything else — a different type, or no
        # edge at all — means the snapshot can no longer be restored from here.
        expected = prior_edge.get("edge_type") or "neural_association"
        if current_type == expected:
            return ShadowEdgeRevert.ALREADY_UNDONE
        return ShadowEdgeRevert.BLOCKED_BY_NEWER_STATE

    # The merge created the edge, so the undo target is "no edge". Absent is
    # the desired end state; a surviving edge of some other type is a later
    # writer's association that this undo must not delete.
    if current_type is None:
        return ShadowEdgeRevert.ALREADY_UNDONE
    return ShadowEdgeRevert.BLOCKED_BY_NEWER_STATE


class UndoMergeError(Exception):
    """A per-merge undo could not be performed.

    Attributes:
        code: Stable machine-readable reason (``action_not_found`` |
            ``not_a_merge`` | ``memory_purged`` | ``already_restored`` |
            ``edge_changed`` | ``not_merge_deleted``).
        message: Human-readable explanation.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: What ``re_embed_memory_to_qdrant`` handed to the embedding sweep (#2061,
#: #2065): nothing, the memory's content chunks, or its point.
RequeuedFor = Literal["chunks", "point"] | None


async def re_embed_memory_to_qdrant(
    memory: Any,
    user_id: str,
    embedding_svc: Any,
    workspace_id: str,
    context_id: str,
    collection_name: str,
    *,
    db: AsyncSession | None = None,
) -> RequeuedFor:
    """Re-embed a restored memory back into Qdrant.

    Merge hard-deletes the loser's vector (orphan-vector prevention), so any
    restore path must rebuild it. Shared by run-level rollback and per-merge
    undo. The point is written by ``write_memory_point``: a row the resource
    indexer wrote (#2053) gets the indexer's own point back under
    ``Memory.point_id`` (the point the merge deleted), rebuilt from the
    document; every other row its summary point. With ``db`` the memory's
    content chunk points (#2015) are rebuilt too when its context has
    ``content_chunks_enabled`` (the merge deleted them with the vector).

    Neither failure aborts the restore while the row can be handed to the
    embedding sweep, in the caller's still-uncommitted restore transaction, so
    the sweep rebuilds what is missing once the restore commits:

    - A failed point write (#2065: a resource whose schema is gone, an
      embedding provider down) re-queues the row ``pending``. The sweep
      rebuilds a resource row through the indexer too, and a resource row that
      cannot be rebuilt ends ``failed`` there, for the admin retry. A row
      that is ``pending`` or ``processing`` already is left to the sweep the
      same way. Any other row (``failed``), or a re-queue that failed, raises
      the write's error: the caller's restore must not commit.
    - A failed chunk write (#2061) re-queues the row the same way. A failed
      re-queue is logged and the restore goes on; the memory then stays
      summary-only until its next re-embed.

    Returns:
        ``"point"`` or ``"chunks"`` when the memory was re-queued for that,
        else None.
    """
    from services.memory_service import write_memory_point
    from services.resource_indexer import owns_resource_point

    if db is None and owns_resource_point(memory):
        raise ValueError(f"Restoring resource-indexed memory {memory.id} needs a database session")
    from services.content_chunk_writer import requeue_embedding, write_content_chunks

    try:
        written = await write_memory_point(
            cast(AsyncSession, db),
            memory,
            collection_name=collection_name,
            embedding_service=embedding_svc,
            user_id=user_id,
            workspace_id=workspace_id,
            context_id=context_id,
        )
    except Exception as e:
        if db is None:
            raise
        logger.warning(
            "restore_point_write_failed",
            memory_id=str(memory.id),
            error_class=type(e).__name__,
        )
        try:
            queued = await requeue_embedding(
                db, memory.id, reason="point_write_failed"
            ) or await _embedding_queued(db, memory.id)
        except Exception:
            queued = False
        if not queued:
            raise
        return "point"

    if written is None or db is None:
        return None
    _vector, payload = written
    try:
        await write_content_chunks(
            db,
            memory,
            base_payload=payload,
            embed_svc=embedding_svc,
            collection_name=collection_name,
            fail_open=False,
        )
    except Exception:
        # Logged by the writer (content_chunks_failed, error class only).
        try:
            if await requeue_embedding(db, memory.id, reason="chunk_write_failed"):
                return "chunks"
        except Exception as e:
            logger.warning(
                "embedding_requeue_failed",
                memory_id=str(memory.id),
                error_class=type(e).__name__,
            )
    return None


async def _embedding_queued(db: AsyncSession, memory_id: Any) -> bool:
    """Whether the embedding sweep has ``memory_id`` queued already (#2065)."""
    status = (
        await db.execute(select(Memory.embedding_status).where(Memory.id == memory_id))
    ).scalar_one_or_none()
    return status in ("pending", "processing")


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
        # #1450: these were one 409 ``already_restored`` until the two zero-row
        # causes were told apart. Both stay 409 (the caller's request conflicts
        # with current state either way) but the codes differ, because the
        # remedies do: "already undone" is a no-op, "blocked" means the merge is
        # STILL IN EFFECT and someone must look at the newer edge.
        if reverted is ShadowEdgeRevert.ALREADY_UNDONE:
            raise UndoMergeError(
                "already_restored",
                f"No supersedes edge {winner_id} → {loser_id} exists — this shadow "
                "merge was already undone.",
            )
        if reverted is ShadowEdgeRevert.BLOCKED_BY_NEWER_STATE:
            raise UndoMergeError(
                "edge_changed",
                f"The edge {winner_id} → {loser_id} was changed or removed by a later "
                "writer, so this shadow merge's pre-merge edge state could not be "
                "restored without discarding that newer state. The merge was not "
                "reversed; the edge needs a look before retrying.",
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
            f"Merged memory {loser_id} no longer exists — {TOMBSTONE_PURGER_CLAUSE}; "
            "whichever window is shorter bounds how long merges stay reversible.",
        )
    if loser.deleted_at is None:
        raise UndoMergeError(
            "already_restored",
            f"Memory {loser_id} is not deleted — this merge was already undone or rolled back.",
        )
    if loser.deleted_by != DELETED_BY_SLEEP_MERGE:
        raise UndoMergeError(
            "not_merge_deleted",
            f"Memory {loser_id} was deleted by '{loser.deleted_by}', not by sleep "
            "maintenance — undo-merge refuses to override a different deletion.",
        )

    embedding_model, collection_name = await _resolve_embedding_info(db, report.context_id)

    # #1520: the same restore UPDATE as run-level rollback — it matches only a
    # live-context row still carrying the merge sentinel, so a forget() or a
    # context soft-delete that landed after the reads above is refused instead
    # of planting a live row + vector where no retention sweep can reach it.
    # #1798: the vector is rebuilt before this UPDATE commits; until then the
    # row still reads as a tombstone to the orphan sweep.
    await hold_point_writer_lock(db)
    restored = await db.execute(
        restore_sleep_tombstone_stmt(loser_id, report.user_id, deleted_by=DELETED_BY_SLEEP_MERGE)
    )
    if cast(CursorResult[Any], restored).rowcount == 0:
        raise UndoMergeError(
            "not_restorable",
            f"Memory {loser_id} could not be restored — it was forgotten, purged, or its "
            "context was deleted after the merge was checked.",
        )
    # #2065: the UPDATE expired the row's computed resource columns (and its
    # embedding clock); the point writer reads them, and an expired attribute
    # cannot be loaded lazily on an async session (MissingGreenlet).
    await db.refresh(loser)

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
    requeued_for = await re_embed_memory_to_qdrant(
        loser,
        report.user_id,
        embedding_svc,
        ws_id,
        ctx_id,
        collection_name,
        db=db,
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
        # #2061 / #2065: "point" when the point write failed and the row was
        # re-queued: the undo stands and the embedding sweep rebuilds it.
        requeued_for=requeued_for,
    )
    return {
        "restored_memory_id": str(loser_id),
        "winner_id": str(winner_id) if winner_id else None,
        "report_id": str(action.report_id),
        "undone_action_id": action.id,
    }
