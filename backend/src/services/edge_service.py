"""Edge-write core shared by the MCP ``create_edge`` tool and the REST
``POST /graph/edges`` endpoint (#1416).

This module owns the transport-agnostic decision logic:

- the deterministic declared-duplicate contract (#1321), and
- the #1403 supersede-candidate accept / self-heal.

It deliberately does NOT own transport concerns. The caller owns the
transaction boundary (``db.commit()`` / ``db.rollback()``), usage telemetry,
and response formatting — so the MCP handler keeps its ``TextContent`` +
``_log_tool_usage`` + ``execute_with_timeout`` wrapper, and the REST route
keeps its HTTP + Pydantic wrapper, while the DB-facing semantics live here
once. ``create_declared_edge`` never commits: it reads/writes on the passed
session and returns a plain-dict :class:`EdgeWriteResult` so the caller can
commit or roll back freely without tripping post-commit ORM expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import (
    EDGE_ORIGIN_DECLARED,
    EDGE_TYPE_CONTINUES_FROM,
    EDGE_TYPE_CONTRADICTS,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_LEARNED_FROM,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    EDGE_TYPE_REFERENCES_FILE,
    EDGE_TYPE_RELATED_TO,
    EDGE_TYPE_SUPERSEDES,
    Memory,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Issue #461 / #741 / #782: full set of edge_types accepted by the DB CHECK
# constraint (`models/memory.py::valid_edge_type`). Sourced from `EDGE_TYPE_*`
# constants so this set cannot drift from the schema literal. Re-exported from
# `mcp_server/tools/edge.py` for backward compatibility with existing importers.
#   - neural_association: runtime Hebbian co-activation, or the post-#741
#     catch-all for any provenance not expressible as a relation
#     (tag_cooccurrence + semantic_similarity merged here).
#   - related_to / depends_on / learned_from: LLM-emittable relation types
#     (#374 → see `services/sleep/edge_discovery.py::LLM_EMITTABLE_EDGE_TYPES`).
#   - continues_from / references_file (#782): producer-asserted structural
#     relation types emitted by the connector-worker ingest pipeline.
#   - supersedes / contradicts (#1208): fact-succession relations. Direction
#     convention: src = superseding (newer), dst = superseded (older) — a
#     memory that is the dst of a live supersedes edge is shadowed out of
#     recall (include_superseded=true opts back in); contradicts never hides.
VALID_EDGE_TYPES = frozenset(
    {
        EDGE_TYPE_NEURAL_ASSOCIATION,
        EDGE_TYPE_RELATED_TO,
        EDGE_TYPE_DEPENDS_ON,
        EDGE_TYPE_LEARNED_FROM,
        EDGE_TYPE_CONTINUES_FROM,
        EDGE_TYPE_REFERENCES_FILE,
        EDGE_TYPE_SUPERSEDES,
        EDGE_TYPE_CONTRADICTS,
    }
)

# Outcome of a declared-edge write. ``conflict`` is the only path that carries
# no ``edge`` (the caller must roll back and surface ``existing``); the other
# three carry the serialized post-state.
EdgeOperation = Literal["created", "updated", "unchanged", "conflict"]


def edge_to_dict(edge: Any) -> dict[str, Any]:
    """Convert a ``NeuralMemoryEdge`` to a JSON-serializable dict.

    ``origin`` is included (#1321) so callers can see the provenance the
    duplicate contract keys on — e.g. that re-asserting a semantic edge
    updates its values but does NOT promote it to 'declared' (the repo's
    sticky-origin upsert keeps non-hebbian origins).
    """
    return {
        "source_id": str(edge.src_id),
        "target_id": str(edge.dst_id),
        "edge_type": edge.edge_type,
        "weight": edge.weight,
        "confidence": edge.confidence,
        "origin": edge.origin,
        "created_at": edge.created_at.isoformat() if edge.created_at else None,
        "last_updated": edge.last_updated.isoformat() if edge.last_updated else None,
    }


@dataclass
class EdgeWriteResult:
    """Transport-agnostic result of :func:`create_declared_edge`.

    ``edge`` is the serialized post-state for ``created`` / ``updated`` /
    ``unchanged`` and ``None`` for ``conflict``. ``previous`` is the pre-image
    dict when an existing (non-protected) edge was upserted. ``existing`` is the
    serialized conflicting declared edge on the ``conflict`` path only. All are
    plain dicts serialized while the ORM was still live, so the caller may
    commit or roll back without hitting a post-rollback ``MissingGreenlet``.
    """

    operation: EdgeOperation
    edge: dict[str, Any] | None
    previous: dict[str, Any] | None = None
    existing: dict[str, Any] | None = None


async def accept_supersede_candidate_if_matching(
    db: AsyncSession, *, src_id: UUID, dst_id: UUID
) -> None:
    """#1403: record acceptance of a suggested supersession and self-heal.

    When a ``supersedes`` edge ``src → dst`` confirms a previously-suggested
    candidate (``src.supersede_candidate.memory_id == dst``), emit the
    ``supersede_suggestion_accepted`` telemetry (the accept side of the
    detected/accepted adoption funnel) and clear the stored suggestion so it
    stops surfacing on recall()/reference(). The mutation is committed by the
    caller's ``db.commit()`` in the same transaction as the edge.

    Best-effort: any failure is swallowed — telemetry/self-heal must never fail
    the edge creation itself.
    """
    try:
        result = await db.execute(select(Memory).where(Memory.id == src_id))
        memory = result.scalar_one_or_none()
        if memory is None or not isinstance(memory.supersede_candidate, dict):
            return
        cand = memory.supersede_candidate
        if cand.get("memory_id") != str(dst_id):
            return
        # Clear the accepted suggestion (server-only column; None = no suggestion).
        memory.supersede_candidate = None
        logger.info(
            "supersede_suggestion_accepted",
            memory_id=str(src_id),
            superseded_memory_id=str(dst_id),
            similarity=cand.get("similarity"),
        )
    except Exception as e:
        logger.warning("supersede_accept_check_failed", error=str(e))


async def create_declared_edge(
    db: AsyncSession,
    *,
    user_id: str,
    source_id: UUID,
    target_id: UUID,
    edge_type: str,
    weight: float,
    confidence: float,
    workspace_id: str,
    context_id: str,
    overwrite: bool,
) -> EdgeWriteResult:
    """Create/update a user-declared edge (shared by MCP + REST, #1321/#1403).

    Callers are responsible for validating ``source_id``/``target_id``/
    ``edge_type``/``weight``/``confidence``/``overwrite`` and for resolving +
    authorizing the (workspace, context) scope before calling this. This does
    the DB-facing decision only and never commits.

    Duplicate behavior on an existing (source, target) pair:
      - existing ``origin != 'declared'`` (hebbian/semantic auto-edge): upsert
        proceeds and the result is ``updated`` plus the pre-image ``previous``.
      - existing ``origin == 'declared'`` with identical edge_type/weight/
        confidence: no write, ``unchanged`` (keeps client retries idempotent).
      - existing ``origin == 'declared'`` with differing values: ``conflict``
        (no write) unless ``overwrite=True`` — declared links are provenance
        (#741) and must not be silently clobbered.

    When a ``supersedes`` edge is written, a matching stored supersede_candidate
    is accepted + cleared in the same transaction (#1403).
    """
    from repositories.neural_edge import NeuralEdgeRepository

    repo = NeuralEdgeRepository(db)

    existing = await repo.get_edge(
        user_id,
        source_id,
        target_id,
        workspace_id=workspace_id,
        context_id=context_id,
    )

    if existing is not None and existing.origin == EDGE_ORIGIN_DECLARED and not overwrite:
        # Snapshot while the instance is still live: a caller's rollback on the
        # conflict path expires ALL loaded ORM state (regardless of
        # expire_on_commit), and a post-rollback attribute access on the async
        # session raises MissingGreenlet (sync lazy refresh).
        existing_snapshot = edge_to_dict(existing)
        if (
            existing.edge_type == edge_type
            and existing.weight == weight
            and existing.confidence == confidence
        ):
            # Idempotent re-assert: same declared edge, same values.
            return EdgeWriteResult(operation="unchanged", edge=existing_snapshot)
        return EdgeWriteResult(operation="conflict", edge=None, existing=existing_snapshot)

    previous = (
        {
            "edge_type": existing.edge_type,
            "weight": existing.weight,
            "confidence": existing.confidence,
            "origin": existing.origin,
        }
        if existing is not None
        else None
    )

    edge = await repo.create_or_update_edge(
        user_id=user_id,
        src_id=source_id,
        dst_id=target_id,
        edge_type=edge_type,
        weight=weight,
        confidence=confidence,
        workspace_id=workspace_id,
        context_id=context_id,
        origin=EDGE_ORIGIN_DECLARED,
        # Without explicit overwrite, keep the declared-type guard on the upsert
        # itself: if a declared edge appears between the get_edge above and this
        # statement (race), its edge_type and origin survive rather than being
        # silently retyped.
        protect_declared_link=not overwrite,
    )

    # #1403: if this supersedes edge confirms a stored suggestion, record the
    # acceptance and clear it (self-heal), in the same transaction.
    if edge_type == EDGE_TYPE_SUPERSEDES:
        await accept_supersede_candidate_if_matching(db, src_id=source_id, dst_id=target_id)

    return EdgeWriteResult(
        operation="updated" if previous is not None else "created",
        edge=edge_to_dict(edge),
        previous=previous,
    )
