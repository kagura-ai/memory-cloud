"""Retrieval feedback service (Issue #888).

Append-only writes of retrieval feedback events + a read-time aggregation
(net-helpful per memory). The feedback lane lives in its own table and is never
embedded, so it cannot pollute ``recall()`` — this service only ever READS the
``memories`` table (to validate the target memory belongs to the context) and
never writes to it or the search index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import AuditLog
from models.memory import Memory
from models.retrieval_feedback import (
    _ALL_FEEDBACK_PROVENANCES,
    FEEDBACK_PROVENANCE_AGENT,
    FEEDBACK_PROVENANCE_HOST,
    NOTE_MAX_LEN,
    QUERY_MAX_LEN,
    RetrievalFeedback,
)
from utils.exceptions import NotFoundException

HOST_VERDICT_SOURCES: tuple[str, ...] = (
    "objective_check",
    "trusted_host_check",
    "hitl_approval",
)
HOST_VERDICT_REFERENCE_MAX_LEN = 1024
HOST_EXPERIMENT_ID_MAX_LEN = 255
AUDIT_HOST_FEEDBACK_RECORDED = "host_feedback_recorded"


@dataclass(frozen=True)
class FeedbackAggregate:
    """Net-helpful tally for a single memory within a context."""

    memory_id: str
    helpful_count: int
    not_helpful_count: int

    @property
    def net(self) -> int:
        return self.helpful_count - self.not_helpful_count


class FeedbackService:
    """Record retrieval feedback events and aggregate them (read-time)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def _stage_feedback(
        self,
        context_id: UUID,
        memory_id: UUID,
        helpful: bool,
        user_id: str,
        query: str | None,
        note: str | None,
        provenance: str,
    ) -> tuple[RetrievalFeedback, UUID | None]:
        """Validate and stage one append-only event without committing it."""
        if provenance not in _ALL_FEEDBACK_PROVENANCES:
            raise ValueError(
                f"invalid feedback provenance {provenance!r}; "
                f"expected one of {_ALL_FEEDBACK_PROVENANCES}"
            )
        existing = (
            await self.db.execute(
                select(Memory.id, Memory.workspace_id).where(
                    Memory.id == memory_id,
                    Memory.context_id == context_id,
                    Memory.deleted_at.is_(None),
                )
            )
        ).one_or_none()
        if existing is None:
            raise NotFoundException("Memory")

        row = RetrievalFeedback(
            context_id=context_id,
            memory_id=memory_id,
            helpful=helpful,
            user_id=user_id,
            query=query[:QUERY_MAX_LEN] if query is not None else None,
            note=note[:NOTE_MAX_LEN] if note is not None else None,
            provenance=provenance,
        )
        self.db.add(row)
        return row, existing.workspace_id

    async def record_feedback(
        self,
        context_id: UUID,
        memory_id: UUID,
        helpful: bool,
        user_id: str,
        query: str | None = None,
        note: str | None = None,
        provenance: str = FEEDBACK_PROVENANCE_AGENT,
    ) -> RetrievalFeedback:
        """Append one feedback event. Query/note are truncated, never embedded.

        Append-only: every call inserts a new row (no upsert) so the signal keeps
        its time series. Returns the persisted row.

        Validates that ``memory_id`` belongs to ``context_id`` (and is not
        soft-deleted) BEFORE inserting — the caller's authorization is on the
        context, so without this a caller could inject feedback for a memory in
        a context they cannot read (cross-context signal injection). A uniform
        ``NotFoundException`` is raised on miss (same IDOR-safe shape as the
        context gate; does not reveal whether the memory exists elsewhere).

        Issue #1065: ``provenance`` is server-stamped. The public feedback path
        (REST/MCP) never passes it, so an agent's signal is always ``agent`` (it
        cannot forge ``host``); only :meth:`record_host_feedback` stamps ``host``.
        """
        row, workspace_id = await self._stage_feedback(
            context_id,
            memory_id,
            helpful,
            user_id,
            query,
            note,
            provenance,
        )
        await self.db.commit()
        await self.db.refresh(row)

        # #1278: append-only audit (no-op unless verified agent identity). Only
        # the public (agent) path is audited; record_host_feedback is the
        # gateway seam, not agent activity.
        if provenance == FEEDBACK_PROVENANCE_AGENT:
            from services.memory_access_event_writer import emit_memory_access_event

            await emit_memory_access_event(
                operation="feedback",
                outcome="success",
                workspace_id=workspace_id,
                user_id=user_id,
                context_id=context_id,
                memory_id=memory_id,
            )
        return row

    async def record_host_feedback(
        self,
        context_id: UUID,
        memory_id: UUID,
        helpful: bool,
        user_id: str,
        query: str | None = None,
        *,
        verdict_source: str = "trusted_host_check",
        verdict_reference: str | None = None,
        experiment_id: str | None = None,
        note: str | None = None,
        actor_email: str | None = None,
        actor_metadata: dict[str, Any] | None = None,
        verdict: str | None = None,
    ) -> RetrievalFeedback:
        """Record a HOST-ARBITRATED, forge-resistant feedback signal (Issue #1065).

        The substrate seam for a trusted host/cockpit to stamp a reinforce signal
        backed by an **independent verdict** (a check/test exit code or an operator
        HITL approval) — never the agent's self-report. Stamps
        ``provenance='host'`` so the re-rank can weight it distinctly from an
        agent's self-emitted ``feedback`` for untrusted callers.

        The caller must identify an allowed independent verdict source and a
        concrete reference. ``verdict`` remains a compatibility alias for the
        old service-only seam; new callers use ``verdict_reference``. The event
        and its security audit row commit atomically.
        """
        if verdict_source not in HOST_VERDICT_SOURCES:
            raise ValueError(
                f"invalid verdict_source {verdict_source!r}; expected one of {HOST_VERDICT_SOURCES}"
            )
        reference = verdict_reference if verdict_reference is not None else verdict
        flat_reference = " ".join(str(reference or "").split())
        if not flat_reference:
            raise ValueError("verdict_reference must identify an independent verdict")
        if len(flat_reference) > HOST_VERDICT_REFERENCE_MAX_LEN:
            raise ValueError(
                f"verdict_reference must be at most {HOST_VERDICT_REFERENCE_MAX_LEN} characters"
            )

        clean_experiment_id = None
        if experiment_id is not None:
            clean_experiment_id = experiment_id.strip()
            if not clean_experiment_id:
                raise ValueError("experiment_id must not be blank")
            if len(clean_experiment_id) > HOST_EXPERIMENT_ID_MAX_LEN:
                raise ValueError(
                    f"experiment_id must be at most {HOST_EXPERIMENT_ID_MAX_LEN} characters"
                )

        # Preserve a compact human-readable copy on the feedback event while
        # keeping the structured, exact attribution in the audit lane.
        prefix = f"host-verdict[{verdict_source}]: "
        suffix = f" | note: {' '.join(note.split())}" if note else ""
        host_note = f"{prefix}{flat_reference}{suffix}"[:NOTE_MAX_LEN]
        row, workspace_id = await self._stage_feedback(
            context_id,
            memory_id,
            helpful,
            user_id,
            query,
            host_note,
            FEEDBACK_PROVENANCE_HOST,
        )
        self.db.add(
            AuditLog(
                user_email=actor_email or f"{user_id}@api",
                user_id=user_id,
                action=AUDIT_HOST_FEEDBACK_RECORDED,
                resource=f"memory:{memory_id}",
                user_metadata={
                    **(actor_metadata or {}),
                    "workspace_id": str(workspace_id) if workspace_id is not None else None,
                    "context_id": str(context_id),
                    "memory_id": str(memory_id),
                    "helpful": helpful,
                    "verdict_source": verdict_source,
                    "verdict_reference": flat_reference,
                    "experiment_id": clean_experiment_id,
                },
            )
        )
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def aggregate_for_memory(
        self, context_id: UUID, memory_id: UUID, *, host_only: bool = False
    ) -> FeedbackAggregate:
        """Net-helpful tally for one memory in a context (read-time only).

        Issue #1065: ``host_only`` counts only host-arbitrated (forge-resistant)
        feedback — the unforgeable signal — for untrusted callers.
        """
        conditions = [
            RetrievalFeedback.context_id == context_id,
            RetrievalFeedback.memory_id == memory_id,
        ]
        if host_only:
            conditions.append(RetrievalFeedback.provenance == FEEDBACK_PROVENANCE_HOST)
        result = await self.db.execute(
            select(
                RetrievalFeedback.helpful,
                func.count().label("n"),
            )
            .where(*conditions)
            .group_by(RetrievalFeedback.helpful)
        )
        helpful_count = 0
        not_helpful_count = 0
        for is_helpful, n in result.all():
            if is_helpful:
                helpful_count = n
            else:
                not_helpful_count = n
        return FeedbackAggregate(
            memory_id=str(memory_id),
            helpful_count=helpful_count,
            not_helpful_count=not_helpful_count,
        )

    async def aggregate_for_memories(
        self, context_id: UUID, memory_ids: list[UUID], *, host_only: bool = False
    ) -> dict[str, FeedbackAggregate]:
        """Batch net-helpful tallies for many memories in a context (one query).

        Issue #1048: the recall reinforce re-rank needs feedback for every
        candidate without N round-trips. Returns a dict keyed by ``str(memory_id)``;
        memories with no feedback are absent (callers treat missing as net=0).

        Issue #1065: ``host_only`` restricts the tally to host-arbitrated
        (provenance='host') feedback — the forge-resistant signal an untrusted
        agent cannot emit. With it set, agent self-emitted feedback contributes
        nothing to ranking.
        """
        if not memory_ids:
            return {}
        conditions = [
            RetrievalFeedback.context_id == context_id,
            RetrievalFeedback.memory_id.in_(memory_ids),
        ]
        if host_only:
            conditions.append(RetrievalFeedback.provenance == FEEDBACK_PROVENANCE_HOST)
        result = await self.db.execute(
            select(
                RetrievalFeedback.memory_id,
                RetrievalFeedback.helpful,
                func.count().label("n"),
            )
            .where(*conditions)
            .group_by(RetrievalFeedback.memory_id, RetrievalFeedback.helpful)
        )
        counts: dict[str, list[int]] = {}  # str(memory_id) -> [helpful, not_helpful]
        for mem_id, is_helpful, n in result.all():
            entry = counts.setdefault(str(mem_id), [0, 0])
            if is_helpful:
                entry[0] = n
            else:
                entry[1] = n
        return {
            mid: FeedbackAggregate(memory_id=mid, helpful_count=h, not_helpful_count=nh)
            for mid, (h, nh) in counts.items()
        }
