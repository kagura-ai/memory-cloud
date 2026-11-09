"""Source-backed, one-hop retrieval hints prepared during Sleep (#2728).

The index stores fingerprints and offsets, never another copy of a fact. It
does not assert causality: a link is only a candidate for a subsequent read.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from models.memory import Memory, NeuralMemoryEdge
from models.sleep import SleepRelatedIndex
from services.sleep.reporter import PhaseResult, SleepBudget, SleepReporter
from utils.datetime import utcnow

MAX_SEEDS = 2
MAX_ADDITIONS = 2
MAX_EDGE_READS = 16
# The phase is DB-only, but it shares the run's memory budget with the LLM
# phases after it: it takes at most a quarter of what remains, two memories
# per pair, and never more than this many new pairs per run.
MAX_INDEX_PAIRS_PER_RUN = 25


def index_enabled(*, reading: bool = False) -> bool:
    """Keep build and consumption independently opt-in without a new MCP field."""
    key = "RECALL_RELATED_INDEX_ENABLED" if reading else "SLEEP_RELATED_INDEX_ENABLED"
    return os.environ.get(key, "false").lower() == "true"


def evidence(memory: Memory) -> dict[str, Any]:
    """Bind exact source text and attribution to an index without copying text."""
    material = {
        "summary": memory.summary,
        "content": memory.content,
        "details": memory.details,
        "source_uri": memory.source_uri,
        "source_type": memory.source_type,
        "type": memory.type,
        "user_id": memory.user_id,
        "context_id": str(memory.context_id),
        "workspace_id": str(memory.workspace_id),
        "version": memory.version,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    return {
        "memory_id": str(memory.id),
        "version": memory.version,
        "fingerprint": digest,
        "spans": [{"field": "summary", "start": 0, "end": len(memory.summary)}],
    }


def current(index: SleepRelatedIndex, source: Memory, target: Memory) -> bool:
    """A changed, erased or moved endpoint cannot contribute indexed evidence."""
    return (
        source.deleted_at is None
        and target.deleted_at is None
        and source.context_id == target.context_id == index.context_id
        and source.workspace_id == target.workspace_id == index.workspace_id
        and index.evidence == {"source": evidence(source), "target": evidence(target)}
    )


class RelatedIndexPhase:
    """Annotate existing discovered edges within the remaining Sleep budget."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def execute(
        self,
        config: Any,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        budget: SleepBudget,
        *,
        reporter: SleepReporter | None = None,
        report_id: UUID | None = None,
    ) -> PhaseResult:
        result = PhaseResult(phase_name="related_index")
        if not index_enabled() or not context_id or not workspace_id or not report_id:
            result.skipped, result.skip_reason = True, "disabled_or_unscoped"
            return result
        remaining = max(0, budget.max_memories - budget.memories_used)
        pair_limit = min(remaining // 4 // 2, MAX_INDEX_PAIRS_PER_RUN)
        if pair_limit < 1:
            result.skipped, result.skip_reason = True, "budget_exhausted"
            return result
        source_row, target_row = aliased(Memory), aliased(Memory)
        rows = await self.db.execute(
            select(NeuralMemoryEdge)
            .join(source_row, source_row.id == NeuralMemoryEdge.src_id)
            .join(target_row, target_row.id == NeuralMemoryEdge.dst_id)
            .outerjoin(SleepRelatedIndex, SleepRelatedIndex.edge_id == NeuralMemoryEdge.id)
            .where(
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.workspace_id == UUID(workspace_id),
                NeuralMemoryEdge.context_id == UUID(context_id),
                NeuralMemoryEdge.origin == "semantic",
                NeuralMemoryEdge.edge_type.in_(["related_to", "semantic_similarity"]),
                # Only edges with no index row yet: an existing row is checked
                # at read time and never re-certified here, so re-reading it
                # would only spend budget.
                SleepRelatedIndex.edge_id.is_(None),
                source_row.deleted_at.is_(None),
                target_row.deleted_at.is_(None),
                # Erasure survivors are intentionally unindexed (#2738 review).
                source_row.embedding_suppressed_at.is_(None),
                target_row.embedding_suppressed_at.is_(None),
                source_row.context_id == UUID(context_id),
                target_row.context_id == UUID(context_id),
                source_row.workspace_id == UUID(workspace_id),
                target_row.workspace_id == UUID(workspace_id),
                # A pre-existing edge cannot certify text edited since discovery.
                or_(
                    source_row.updated_at.is_(None),
                    source_row.updated_at <= NeuralMemoryEdge.created_at,
                ),
                or_(
                    target_row.updated_at.is_(None),
                    target_row.updated_at <= NeuralMemoryEdge.created_at,
                ),
            )
            .order_by(NeuralMemoryEdge.id)
            .limit(pair_limit)
        )
        edges = [row[0] for row in rows.all()]
        ids = {node for edge in edges for node in (edge.src_id, edge.dst_id)}
        memories = {
            m.id: m
            for m in (
                await self.db.scalars(
                    select(Memory).where(
                        Memory.id.in_(ids),
                        Memory.deleted_at.is_(None),
                        Memory.workspace_id == UUID(workspace_id),
                        Memory.context_id == UUID(context_id),
                    )
                )
            ).all()
        }
        written = 0
        indexed_ids: set[UUID] = set()
        for edge in edges:
            source, target = memories.get(edge.src_id), memories.get(edge.dst_id)
            if source is None or target is None or source.id == target.id:
                continue
            existing = SleepRelatedIndex(
                edge_id=edge.id,
                source_id=source.id,
                target_id=target.id,
                context_id=source.context_id,
                workspace_id=source.workspace_id,
            )
            self.db.add(existing)
            existing.evidence = {"source": evidence(source), "target": evidence(target)}
            existing.report_id = report_id
            existing.checked_at = utcnow()
            if reporter:
                await reporter.add_action(
                    report_id=report_id,
                    phase="related_index",
                    action_type="index_related",
                    memory_id=source.id,
                    target_id=target.id,
                    details={"edge_id": edge.id, "previous": None},
                )
            written += 1
            indexed_ids.update((source.id, target.id))
        # Charged to the run's budget: only the memories this run indexed.
        result.memories_processed = len(indexed_ids)
        result.details = {"candidates": len(edges), "indexed": written}
        return result


async def candidate_links(
    db: AsyncSession, seeds: list[Memory], *, context_id: UUID, workspace_id: UUID
) -> list[SleepRelatedIndex]:
    """Fetch a bounded set of indexed edges; endpoint validation follows separately."""
    ids = [m.id for m in seeds[:MAX_SEEDS]]
    superseder = aliased(Memory)
    supersedes = aliased(NeuralMemoryEdge)
    live_supersession = (
        select(supersedes.id)
        .join(superseder, superseder.id == supersedes.src_id)
        .where(
            supersedes.edge_type == "supersedes",
            supersedes.dst_id.in_([SleepRelatedIndex.source_id, SleepRelatedIndex.target_id]),
            superseder.deleted_at.is_(None),
        )
        .correlate(SleepRelatedIndex)
        .exists()
    )
    return list(
        (
            await db.scalars(
                select(SleepRelatedIndex)
                .join(NeuralMemoryEdge, NeuralMemoryEdge.id == SleepRelatedIndex.edge_id)
                .where(
                    SleepRelatedIndex.context_id == context_id,
                    SleepRelatedIndex.workspace_id == workspace_id,
                    NeuralMemoryEdge.context_id == context_id,
                    NeuralMemoryEdge.workspace_id == workspace_id,
                    NeuralMemoryEdge.src_id == SleepRelatedIndex.source_id,
                    NeuralMemoryEdge.dst_id == SleepRelatedIndex.target_id,
                    NeuralMemoryEdge.origin == "semantic",
                    NeuralMemoryEdge.edge_type.in_(["related_to", "semantic_similarity"]),
                    ~live_supersession,
                    or_(SleepRelatedIndex.source_id.in_(ids), SleepRelatedIndex.target_id.in_(ids)),
                )
                .order_by(NeuralMemoryEdge.weight.desc(), SleepRelatedIndex.edge_id)
                .limit(MAX_EDGE_READS)
            )
        ).all()
    )


async def undo_related_index(db: Any, action: Any, ctx: Any, summary: dict[str, Any]) -> None:
    """Undo only the index revision written by this run; retain later rebuilds."""
    edge_id = (action.details or {}).get("edge_id")
    row = await db.scalar(
        select(SleepRelatedIndex)
        .where(
            SleepRelatedIndex.edge_id == edge_id,
            SleepRelatedIndex.context_id == UUID(ctx.ctx_id_str),
            SleepRelatedIndex.workspace_id == UUID(ctx.ws_id),
            SleepRelatedIndex.source_id == action.memory_id,
            SleepRelatedIndex.target_id == action.target_id,
        )
        .with_for_update()
    )
    if row is None or row.report_id != action.report_id:
        return
    previous = (action.details or {}).get("previous")
    if previous is None:
        await db.delete(row)
    else:
        row.evidence = previous["evidence"]
        row.report_id = UUID(previous["report_id"]) if previous.get("report_id") else None
    summary["related_indexes_reverted"] = summary.get("related_indexes_reverted", 0) + 1
