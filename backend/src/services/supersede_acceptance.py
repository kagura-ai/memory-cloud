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
  would hide the newer memory behind the older one.

The edge writer (``edge_service.ensure_supersedes_owned``) stays the
authority on click and keeps its own uniform refusal; the reason reported
here is as coarse: ``not_owner`` never says which memory is foreign.

Known residual: an agent-bound API key whose binding is read-only passes the
probe here (a binding is not a workspace role) and is refused on accept with
the uniform 404 it already gets. The web dialog, a session caller, is not
bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.memory import Memory
from models.schemas import SupersedeAcceptance
from services.memory_ownership import own_memory_predicate
from utils.exceptions import AuthorizationError, FeatureNotAvailableError

AcceptanceReason = Literal["not_writable", "not_owner", "newer_candidate"]


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


def acceptance_reason(pair: PairOwnership, *, can_write: bool) -> AcceptanceReason | None:
    """Why the pair may not be accepted, or None when it may.

    Precedence: ``not_writable`` > ``not_owner`` > ``newer_candidate``. A
    missing target is ``not_owner``, as the edge writer's uniform refusal
    treats it; the accept path tells it apart itself.
    """
    if not can_write:
        return "not_writable"
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
    role of ``member`` or higher (with a plan seat) and a writable context
    slot. Not the full context resolver: that logs a WARNING on every deny,
    and a viewer opening a memory with a suggestion is not a denied request.
    """
    from services.permission_service import PermissionService
    from services.plan_seats import ensure_context_writable

    try:
        await PermissionService(db).check_workspace_access(
            user_id, workspace_id, required_role=WorkspaceRole.MEMBER
        )
        await ensure_context_writable(db, context_id)
    except (AuthorizationError, FeatureNotAvailableError):
        return False
    return True


async def evaluate(
    db: AsyncSession, *, user_id: str, memory: Any, target_id: UUID
) -> SupersedeAcceptance:
    """The ``acceptance`` for ``memory``'s current candidate ``target_id``."""
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
    reason = acceptance_reason(pair, can_write=True)
    return SupersedeAcceptance(allowed=reason is None, reason=reason)
