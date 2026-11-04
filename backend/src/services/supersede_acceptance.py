"""Whether the caller may accept a memory's supersede suggestion (#2427).

One rule, four consumers: ``reference()``, the PATCH reply and, #2508, every
``recall()`` row report it as ``supersede_candidate.acceptance`` so a client
can disable an action the server would refuse, and
``update_memory(accept_supersede_candidate=true)`` enforces it. A recall page
is evaluated by :func:`evaluate_many` in a bounded number of queries; the
single-memory :func:`evaluate` is that batch over one memory, so the page and
the single read cannot drift. The rule is the accept path's:

* write access to the context: a workspace role of ``member`` or higher and a
  writable context slot (#1943) — the part of the ``POST /graph/edges`` gate
  that a caller who has already read the memory has not proven yet. On a
  memory that carries a tool guardrail (``details.tool_trigger``) also a user
  credential and context EDITOR or above (#2614): the gate both accept
  transports run first, as on any edit of a guardrail;
* both memories the caller's own (#1803/#2313): in a private context a linked
  account counts, in a shared one only the caller;
* a candidate not written after the memory carrying it (#2308): accepting it
  would hide the newer memory behind the older one;
* no different declared edge on the pair kept by the carrier's author
  (#2518): the edge writer's duplicate contract (#1321) refuses to retype it,
  the accept path's 409. Both transports write the accept under that author
  (#2600), so a row kept by any other account, the caller's included, is not
  one the accept meets (#2614, settling #2549's two-account read).

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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import ACCESS_WRITE, WorkspaceRole
from models.memory import EDGE_ORIGIN_DECLARED, EDGE_TYPE_SUPERSEDES, Memory
from models.schemas import SupersedeAcceptance
from services.memory_ownership import is_private_context, own_memory_clause
from utils.exceptions import AuthorizationError, FeatureNotAvailableError
from utils.tool_trigger import carries_tool_trigger

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
    same-context by construction (``_resolve_supersede_candidates``). The
    accept path's read; :func:`pair_ownerships` over one pair.
    """
    private = await is_private_context(db, context_id)
    pairs = await pair_ownerships(
        db, user_id=user_id, private=private, pairs={carrier_id: target_id}
    )
    return pairs[carrier_id]


async def pair_ownerships(
    db: AsyncSession,
    *,
    user_id: str,
    private: bool,
    pairs: Mapping[UUID, UUID],
) -> dict[UUID, PairOwnership]:
    """:class:`PairOwnership` of each carrier → candidate pair, in one query.

    The pairs are one context's: ``private`` is that context's
    :func:`services.memory_ownership.is_private_context`, which decides
    whether linked accounts count, so a page spanning contexts calls this
    once per context (#2508).
    """
    own = own_memory_clause(user_id, private=private)
    ids = {*pairs, *pairs.values()}
    rows = {
        row.id: row
        for row in (
            await db.execute(
                select(Memory.id, Memory.created_at, own.label("own")).where(Memory.id.in_(ids))
            )
        ).all()
    }
    owned: dict[UUID, PairOwnership] = {}
    for carrier_id, target_id in pairs.items():
        carrier = rows.get(carrier_id)
        target = rows.get(target_id)
        owned[carrier_id] = PairOwnership(
            carrier_own=bool(carrier is not None and carrier.own),
            target_own=bool(target is not None and target.own),
            carrier_created_at=carrier.created_at if carrier is not None else None,
            target_created_at=target.created_at if target is not None else None,
            target_exists=target is not None,
        )
    return owned


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


async def may_author_guardrail(
    db: AsyncSession, *, user_id: str, context_id: UUID | str | None
) -> bool:
    """Whether the caller passes the guardrail author gate in the context (#2614).

    The probe twin of ``MemoryService._require_guardrail_author``, which
    ``update_memory`` and ``PATCH /memory/{id}`` run before the accept on a
    memory carrying ``details.tool_trigger``: a user credential and context
    EDITOR or above (workspace owner/admin, a ContextMember editor/owner, the
    creator of a private context or an account linked to it). An agent
    credential is answered before the context check, whose binding filter
    would audit an agent's deny; the user-credential refusal a reader of the
    memory meets there is the RBAC core's 403, which logs nothing.
    """
    from auth.agent_scope import get_agent_scope
    from services.permission_service import PermissionService

    if get_agent_scope() is not None or context_id is None:
        return False
    return await PermissionService(db).can_write_context(user_id, UUID(str(context_id)))


class EdgeProbe(NamedTuple):
    """One carrier's pair for :func:`conflicting_declared_edges`."""

    owner: str
    target_id: UUID
    workspace_id: UUID
    context_id: UUID


def _conflicts_with_accept(row: Any) -> bool:
    """A declared row with other values than the accept's: its 409 (#2518)."""
    return row.origin == EDGE_ORIGIN_DECLARED and not (
        row.edge_type == EDGE_TYPE_SUPERSEDES
        and row.weight == ACCEPT_WEIGHT
        and row.confidence == ACCEPT_CONFIDENCE
    )


async def conflicting_declared_edges(
    db: AsyncSession, probes: Mapping[UUID, EdgeProbe]
) -> set[UUID]:
    """The carriers whose accept would meet a different declared edge.

    The lookup ``create_declared_edge`` makes before it writes, under a
    probe's ``owner``: the carrier's author, the account both accept
    transports write under (#2600, #2614). A declared row with other values
    than the accept's (another type, weight or confidence) is a ``conflict``
    there — refused with a 409, nothing written. An identical declared
    ``supersedes`` row is ``unchanged`` and an automatic (hebbian/semantic)
    row is overwritten, so neither refuses.

    One query for every pair (#2508), each held to its own workspace and
    context; nothing is read for no pairs.
    """
    from repositories.neural_edge import NeuralEdgeRepository

    if not probes:
        return set()
    keys = [
        (probe.owner, carrier_id, probe.target_id, probe.workspace_id, probe.context_id)
        for carrier_id, probe in probes.items()
    ]
    rows = await NeuralEdgeRepository(db).list_pairs_edges(keys)
    return {
        row.src_id
        for row in rows
        if row.src_id in probes
        and row.dst_id == probes[row.src_id].target_id
        and _conflicts_with_accept(row)
    }


async def evaluate(
    db: AsyncSession, *, user_id: str, memory: Any, target_id: UUID
) -> SupersedeAcceptance:
    """The ``acceptance`` for ``memory``'s current candidate ``target_id``.

    :func:`evaluate_many` over the one memory: the single read and a recall
    page answer from the same code.
    """
    acceptances = await evaluate_many(
        db, user_id=user_id, memories={memory.id: memory}, targets={memory.id: target_id}
    )
    return acceptances[memory.id]


async def evaluate_many(
    db: AsyncSession,
    *,
    user_id: str,
    memories: Mapping[UUID, Any],
    targets: Mapping[UUID, UUID],
) -> dict[UUID, SupersedeAcceptance]:
    """The ``acceptance`` of each carrier's current candidate, keyed by carrier id.

    ``targets`` maps a carrier id to its candidate's id; each carrier among
    ``memories`` that has one gets an answer. Reasons in the order the accept
    path refuses: ``not_writable`` > ``not_owner`` > ``newer_candidate`` >
    ``edge_exists``; each step after the first reads only the carriers still
    allowed, and costs the same for one row or a hundred (#2508):

    1. write access (:func:`can_write_edges`) once per distinct (workspace,
       context) of the carriers; for the carriers of a tool guardrail
       (``details.tool_trigger``), the guardrail author gate
       (:func:`may_author_guardrail`, #2614) once per context too, read only
       where such a carrier is still allowed;
    2. ownership and creation times once per distinct context, over all its
       carriers and their candidates: linked accounts count only in a private
       context, read once per context too. A recall page is one context in
       practice, so this is one ownership query per page;
    3. the declared-edge conflicts (:func:`conflicting_declared_edges`) in one
       query for every remaining pair. ``edge_exists`` reads the carrier
       author's row, the account both transports write the accept under.
    """
    carriers = {cid: memories[cid] for cid in targets if cid in memories}
    reasons: dict[UUID, AcceptanceReason] = {}

    writable: dict[tuple[Any, Any], bool] = {}
    for carrier_id, memory in carriers.items():
        scope = (memory.workspace_id, memory.context_id)
        if scope not in writable:
            writable[scope] = await can_write_edges(
                db, user_id=user_id, workspace_id=scope[0], context_id=scope[1]
            )
        if not writable[scope]:
            reasons[carrier_id] = "not_writable"

    # The accept transports gate a guardrail carrier before the accept.
    guardrail_author: dict[Any, bool] = {}
    for carrier_id, memory in carriers.items():
        if carrier_id in reasons or not carries_tool_trigger(memory.details):
            continue
        if memory.context_id not in guardrail_author:
            guardrail_author[memory.context_id] = await may_author_guardrail(
                db, user_id=user_id, context_id=memory.context_id
            )
        if not guardrail_author[memory.context_id]:
            reasons[carrier_id] = "not_writable"

    by_context: dict[Any, dict[UUID, UUID]] = {}
    for carrier_id, memory in carriers.items():
        if carrier_id not in reasons:
            by_context.setdefault(memory.context_id, {})[carrier_id] = targets[carrier_id]
    for context_id, pairs in by_context.items():
        private = await is_private_context(db, context_id)
        owned = await pair_ownerships(db, user_id=user_id, private=private, pairs=pairs)
        for carrier_id, pair in owned.items():
            if (reason := acceptance_reason(pair)) is not None:
                reasons[carrier_id] = reason

    # Last, as the accept path meets it last: only a pair it would write.
    probes = {
        carrier_id: EdgeProbe(
            owner=memory.user_id,
            target_id=targets[carrier_id],
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
        )
        for carrier_id, memory in carriers.items()
        if carrier_id not in reasons
    }
    if probes:
        for carrier_id in await conflicting_declared_edges(db, probes):
            reasons[carrier_id] = "edge_exists"

    return {
        carrier_id: SupersedeAcceptance(
            allowed=carrier_id not in reasons, reason=reasons.get(carrier_id)
        )
        for carrier_id in carriers
    }
