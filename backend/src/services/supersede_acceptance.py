"""Whether the caller may accept a memory's supersede suggestion (#2427).

One rule, three consumers: ``reference()`` and the PATCH reply report it as
``supersede_candidate.acceptance`` so a client can disable an action the
server would refuse, and ``update_memory(accept_supersede_candidate=true)``
enforces it. The rule is the accept path's:

* write access to the context: a workspace role of ``member`` or higher and a
  writable context slot (#1943) — the part of the ``POST /graph/edges`` gate
  that a caller who has already read the memory has not proven yet;
* both memories the caller's own (#1803/#2313): in a private context a linked
  account counts, in a shared one only the caller;
* a candidate not written after the memory carrying it (#2308): accepting it
  would hide the newer memory behind the older one;
* no different declared edge already on the pair (#2518): the edge writer's
  duplicate contract (#1321) refuses to retype it, the accept path's 409. In a
  private context any account of the carrier author's identity-link set
  counts (#2549), as for ownership.

The edge writer (``edge_service.ensure_supersedes_owned``) stays the
authority on click and keeps its own uniform refusal; the reason reported
here is as coarse: ``not_owner`` never says which memory is foreign.

Write access includes the request's agent binding (#2518): an agent-bound API
key whose binding is read-only for the context is ``not_writable``, as the
accept path's uniform 404 says on click. The probe reads the binding without
the enforcing evaluation's WARNING or deny-audit row: a client asking whether
to show the action is not a denied request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import ACCESS_WRITE, WorkspaceRole
from models.memory import Memory
from models.schemas import SupersedeAcceptance
from services.identity_link_service import linked_user_ids
from services.memory_ownership import is_private_context, own_memory_predicate
from utils.exceptions import AuthorizationError, FeatureNotAvailableError

AcceptanceReason = Literal["not_writable", "not_owner", "newer_candidate", "edge_exists"]

# The values the accept path writes its ``supersedes`` edge with:
# ``create_edge``'s defaults for a user assertion (#738). A declared edge on
# the pair with these values is ``unchanged``, with any others a conflict.
ACCEPT_WEIGHT = 1.0
ACCEPT_CONFIDENCE = 1.0


@dataclass(frozen=True)
class PairOwnership:
    """The carrier/target facts the rule reads (never on the wire)."""

    carrier_own: bool
    target_own: bool
    carrier_created_at: datetime | None
    target_created_at: datetime | None
    target_exists: bool


async def pair_ownership(
    db: AsyncSession,
    *,
    user_id: str,
    context_id: UUID | str | None,
    carrier_id: UUID,
    target_id: UUID,
) -> PairOwnership:
    """Ownership and creation time of the carrier and its candidate.

    ``context_id`` is the carrier's: it decides whether linked accounts count
    (:func:`services.memory_ownership.own_memory_predicate`). The candidate is
    same-context by construction (``_resolve_supersede_candidates``).
    """
    own = await own_memory_predicate(db, context_id, user_id)
    rows = {
        row.id: row
        for row in (
            await db.execute(
                select(Memory.id, Memory.created_at, own.label("own")).where(
                    Memory.id.in_([carrier_id, target_id])
                )
            )
        ).all()
    }
    carrier = rows.get(carrier_id)
    target = rows.get(target_id)
    return PairOwnership(
        carrier_own=bool(carrier is not None and carrier.own),
        target_own=bool(target is not None and target.own),
        carrier_created_at=carrier.created_at if carrier is not None else None,
        target_created_at=target.created_at if target is not None else None,
        target_exists=target is not None,
    )


def acceptance_reason(pair: PairOwnership) -> AcceptanceReason | None:
    """Why the pair may not be accepted, or None when it may.

    Ownership before direction: ``not_owner`` > ``newer_candidate``. A
    missing target is ``not_owner``, as the edge writer's uniform refusal
    treats it; the accept path tells it apart itself. Write access is not a
    property of the pair: :func:`evaluate` checks it first (``not_writable``
    wins over both), the accept path's transport has already checked it.
    """
    if not (pair.target_exists and pair.carrier_own and pair.target_own):
        return "not_owner"
    if (
        pair.target_created_at is not None
        and pair.carrier_created_at is not None
        and pair.target_created_at > pair.carrier_created_at
    ):
        return "newer_candidate"
    return None


async def can_write_edges(
    db: AsyncSession,
    *,
    user_id: str,
    workspace_id: UUID,
    context_id: UUID | str,
) -> bool:
    """Whether a caller who already reads the context may write an edge in it.

    The delta of ``POST /graph/edges``'s gate over the read gate: a workspace
    role of ``member`` or higher (with a plan seat), a writable context slot
    and, for an agent-bound API key, a binding that grants the write (#2518).
    Not the full context resolver: that logs a WARNING (and an agent deny
    writes an audit row) on every deny, and a viewer opening a memory with a
    suggestion is not a denied request.
    """
    from services.agent_binding_service import agent_binding_would_permit
    from services.permission_service import PermissionService
    from services.plan_seats import ensure_context_writable

    try:
        await PermissionService(db).check_workspace_access(
            user_id, workspace_id, required_role=WorkspaceRole.MEMBER
        )
        await ensure_context_writable(db, context_id)
    except (AuthorizationError, FeatureNotAvailableError):
        return False
    return await agent_binding_would_permit(db, UUID(str(context_id)), ACCESS_WRITE)


async def edge_owner(
    db: AsyncSession, *, author_id: str, context_id: UUID | str | None
) -> str | frozenset[str]:
    """Whose edges on the pair the accept can meet (#2549).

    Ownership as :func:`services.memory_ownership.own_memory_predicate`
    resolves it. In a private context the carrier author's identity-link set,
    which holds the caller whenever ``not_owner`` passed: the accept is
    written under the carrier's author (#1784), the web dialog's
    ``POST /graph/edges`` under the caller, and ``unique_edge`` is per
    account, so each of them can keep a row for the pair. Elsewhere the
    author alone, who is then the caller.
    """
    if await is_private_context(db, context_id):
        return await linked_user_ids(db, author_id)
    return author_id


async def conflicting_declared_edge(
    db: AsyncSession,
    *,
    owner: str | frozenset[str],
    workspace_id: UUID,
    context_id: UUID,
    carrier_id: UUID,
    target_id: UUID,
) -> bool:
    """Whether the accept would meet a different declared edge on the pair.

    The lookup ``create_declared_edge`` makes before it writes, made for
    every account in ``owner`` (:func:`edge_owner`), whichever of them the
    click writes under: each row ``owner`` keeps from the carrier to the
    target, not only the strongest one a link-set ``get_edge`` returns
    (#2549). A declared row with other values than the
    accept's (another type, weight or confidence) is a ``conflict`` there —
    refused with a 409, nothing written. An identical declared ``supersedes``
    row is ``unchanged`` and an automatic (hebbian/semantic) row is
    overwritten, so neither refuses.
    """
    from models.memory import EDGE_ORIGIN_DECLARED, EDGE_TYPE_SUPERSEDES
    from repositories.neural_edge import NeuralEdgeRepository

    rows = await NeuralEdgeRepository(db).list_pair_edges(
        owner,
        carrier_id,
        target_id,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
    )
    return any(
        row.origin == EDGE_ORIGIN_DECLARED
        and not (
            row.edge_type == EDGE_TYPE_SUPERSEDES
            and row.weight == ACCEPT_WEIGHT
            and row.confidence == ACCEPT_CONFIDENCE
        )
        for row in rows
    )


async def evaluate(
    db: AsyncSession, *, user_id: str, memory: Any, target_id: UUID
) -> SupersedeAcceptance:
    """The ``acceptance`` for ``memory``'s current candidate ``target_id``.

    Reasons in the order the accept path refuses: ``not_writable`` >
    ``not_owner`` > ``newer_candidate`` > ``edge_exists``. Each check after
    the first runs only when the earlier ones pass. ``edge_exists`` reads the
    pair's edges under the carrier's author, or in a private context under
    the author's identity-link set (#2549): a different declared edge kept by
    any of those accounts refuses.
    """
    if not await can_write_edges(
        db,
        user_id=user_id,
        workspace_id=memory.workspace_id,
        context_id=memory.context_id,
    ):
        return SupersedeAcceptance(allowed=False, reason="not_writable")
    pair = await pair_ownership(
        db,
        user_id=user_id,
        context_id=memory.context_id,
        carrier_id=memory.id,
        target_id=target_id,
    )
    reason: AcceptanceReason | None = acceptance_reason(pair)
    # Last, as the accept path meets it last: only a pair it would write.
    if reason is None and await conflicting_declared_edge(
        db,
        owner=await edge_owner(db, author_id=memory.user_id, context_id=memory.context_id),
        workspace_id=memory.workspace_id,
        context_id=memory.context_id,
        carrier_id=memory.id,
        target_id=target_id,
    ):
        reason = "edge_exists"
    return SupersedeAcceptance(allowed=reason is None, reason=reason)
