"""The pair writes Sleep's dedup and supersede-apply phases share (#3020).

Both phases decide on two memory rows read earlier in the run and then write a
``supersedes`` edge from the newer one to the older one. Two steps are shared
so the two phases cannot drift:

* :func:`locked_refusals` re-reads both rows under ``FOR UPDATE`` right before
  the write and names every reason the write must not happen (the rules of
  #1519, #1524, #2996 and #3019);
* :func:`write_sleep_supersedes_edge` upserts the ``supersedes`` edge, origin
  ``semantic``, and snapshots an edge of another type it retyped, so a
  rollback can put that edge back.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import (
    EDGE_ORIGIN_SEMANTIC,
    EDGE_TYPE_SUPERSEDES,
    MEMORY_TYPE_TIME,
    Memory,
    NeuralMemoryEdge,
    pinned_predicate,
    resource_owned_predicate,
    tool_triggered_predicate,
)
from repositories.neural_edge import NeuralEdgeRepository
from services.supersede_verify import CONTENT_HEAD_CHARS


def _edited_since_fetch(memory: Any, locked_version: object) -> bool:
    """The locked row's version is not the one ``memory`` was fetched with (#3019).

    Both must be ints (bool excluded) for the comparison to mean anything; a
    test double without one reads as unchanged.
    """
    fetched = getattr(memory, "version", None)
    return (
        isinstance(fetched, int)
        and not isinstance(fetched, bool)
        and isinstance(locked_version, int)
        and not isinstance(locked_version, bool)
        and fetched != locked_version
    )


async def locked_refusals(
    db: AsyncSession, rows: Sequence[tuple[str, Any]]
) -> tuple[list[str], dict[UUID, Any]]:
    """Re-check the pair's rows under a row lock right before writing.

    The candidates were read earlier in the run; another session can pin
    (``delivery_mode='always'``), forget, edit or move a row in between.
    ``SELECT ... FOR UPDATE`` inside the phase transaction serializes against
    a concurrent ``update_memory``/``forget``, so the state read here is the
    state the write acts on. Per row, in this order, the first that holds:
    ``<label>_missing``, ``_deleted``, ``_pinned``, ``_tool_triggered``,
    ``_resource_owned`` (#2996), ``_time`` (#1524, ``update_memory`` can flip
    ``type``), ``_moved`` (#3019, another user or context than the one it was
    read in) and ``_edited`` (#3019, its version moved).

    Args:
        db: The phase's session.
        rows: ``(label, obj)`` pairs; ``obj`` has ``id``, ``user_id``,
            ``context_id`` and ``version`` as they were read (a ``Memory`` or
            a column row).

    Returns:
        ``(reasons, locked)``: the reasons (empty when the write may go
        ahead) and the locked rows by id, which also carry ``tags``,
        ``supersede_candidate``, ``summary`` and ``content_head`` (the first
        ``CONTENT_HEAD_CHARS`` of the content).
    """
    locked = (
        await db.execute(
            select(
                Memory.id,
                pinned_predicate().label("pinned"),
                tool_triggered_predicate().label("tool_triggered"),
                resource_owned_predicate().label("resource_owned"),
                Memory.deleted_at,
                Memory.type,
                # #3019: the partition the row was read in.
                Memory.user_id,
                Memory.context_id,
                # #3019: every versioned edit moves it.
                Memory.version,
                # #3020: what the supersede-apply phase decides on again under
                # the lock (dedup ignores them).
                Memory.tags,
                Memory.supersede_candidate,
                Memory.summary,
                func.substr(Memory.content, 1, CONTENT_HEAD_CHARS).label("content_head"),
            )
            .where(Memory.id.in_([obj.id for _label, obj in rows]))
            .with_for_update()
        )
    ).all()
    state = {row.id: row for row in locked}
    reasons: list[str] = []
    for label, obj in rows:
        row = state.get(obj.id)
        if row is None:
            reasons.append(f"{label}_missing")
        elif row.deleted_at is not None:
            reasons.append(f"{label}_deleted")
        elif row.pinned:
            reasons.append(f"{label}_pinned")
        elif row.tool_triggered:
            reasons.append(f"{label}_tool_triggered")
        elif row.resource_owned:
            reasons.append(f"{label}_resource_owned")
        elif row.type == MEMORY_TYPE_TIME:
            reasons.append(f"{label}_time")
        elif row.user_id != obj.user_id or row.context_id != obj.context_id:
            reasons.append(f"{label}_moved")
        elif _edited_since_fetch(obj, row.version):
            reasons.append(f"{label}_edited")
    return reasons, state


async def write_sleep_supersedes_edge(
    edge_repo: NeuralEdgeRepository,
    *,
    user_id: str,
    newer_id: UUID,
    older_id: UUID,
    workspace_id: str | None,
    context_id: str | None,
    edge_metadata: dict[str, Any] | None = None,
    protect_declared_link: bool = False,
) -> tuple[dict[str, Any] | None, NeuralMemoryEdge]:
    """Upsert the ``supersedes`` edge ``newer -> older`` a Sleep phase decided on.

    Origin ``semantic`` (machine-inferred, not user-asserted), weight and
    confidence 1.0. ``unique_edge`` is keyed on (user, src, dst) regardless of
    ``edge_type``, so an edge of another type on the pair (a Hebbian
    association between near-duplicates is the common case) is retyped, and
    its state before is returned as ``prior_edge`` for the action's audit row.
    An existing ``supersedes`` row is not snapshotted: undoing would only
    "restore" ``supersedes``.

    Args:
        edge_repo: The phase's edge repository.
        user_id: The run's user; the edge's owner.
        newer_id: The superseding memory (src).
        older_id: The superseded memory (dst).
        workspace_id: The run's workspace.
        context_id: The run's context.
        edge_metadata: Written to the edge (the upsert replaces the row's
            metadata with it); dedup writes none.
        protect_declared_link: Keep a declared row's type and origin
            (``create_or_update_edge``). A caller that protects one reads the
            written row back (the lookup before the write put the row in the
            identity map), so it can tell whether the guard kept a declared row.

    Returns:
        ``(prior_edge, edge)``: the snapshot of a retyped edge or None, and
        the written row (fresh only with ``protect_declared_link``).
    """
    prior = await edge_repo.get_edge(user_id, newer_id, older_id, workspace_id, context_id)
    prior_edge: dict[str, Any] | None = None
    if prior is not None and prior.edge_type != EDGE_TYPE_SUPERSEDES:
        prior_edge = {
            "edge_type": prior.edge_type,
            "origin": prior.origin,
            "weight": prior.weight,
            "confidence": prior.confidence,
            "edge_metadata": prior.edge_metadata,
        }
    edge = await edge_repo.create_or_update_edge(
        user_id=user_id,
        src_id=newer_id,
        dst_id=older_id,
        edge_type=EDGE_TYPE_SUPERSEDES,
        weight=1.0,
        confidence=1.0,
        workspace_id=workspace_id,
        context_id=context_id,
        origin=EDGE_ORIGIN_SEMANTIC,
        edge_metadata=edge_metadata,
        protect_declared_link=protect_declared_link,
        return_fresh_edge=protect_declared_link,
    )
    return prior_edge, edge
