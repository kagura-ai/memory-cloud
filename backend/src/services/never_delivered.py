"""The never-delivered set (#3022): the one place the rule is written.

A live memory is "never delivered" for viewer V at instant ``now`` when it is
V's own (V or an account linked to V), was created more than
``NEVER_DELIVERED_AGE_DAYS`` ago, has no delivery stamp and no earlier use
signal (``access_count = 0``, ``reference_count = 0``, ``last_used_at`` unset:
rows written before delivery recording began count as delivered when they were
used), and is not served by a lane that records no delivery or owned by
another process: not pinned, not a time memory, not resource-owned, without an
exempt tag, not a tool guardrail, not in a public context, and not the newer
side of a ``supersedes`` edge.

The MCP ``list`` filter, the REST list filter and the context count all call
:func:`never_delivered_predicates`, so the three agree by construction. The
clauses are emitted cheap column tests first, the ``details`` JSON test after
them and the two subqueries last.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta

from sqlalchemy import and_, literal, not_, select
from sqlalchemy.sql import ColumnElement

from config.constants import NEVER_DELIVERED_AGE_DAYS
from models.auth import Context
from models.memory import (
    EDGE_TYPE_SUPERSEDES,
    MEMORY_TYPE_TIME,
    Memory,
    NeuralMemoryEdge,
    not_pinned_predicate,
    not_resource_owned_predicate,
    not_tool_triggered_predicate,
)
from services.exempt_tags import exempt_tags, not_exempt_tagged_predicate
from services.identity_link_service import owned_by
from utils.datetime import utcnow


def never_delivered_predicates(
    viewer_user_id: str,
    *,
    now: datetime | None = None,
    exempt: Collection[str] | None = None,
) -> list[ColumnElement[bool]]:
    """Predicates on ``Memory`` selecting the never-delivered rows of ``viewer_user_id``.

    The scope every list applies (live rows, the context, the private-context
    owner filter, the agent binding) is not part of this: the caller ANDs both.

    Args:
        viewer_user_id: The caller; only their own rows (linked accounts
            included) are in the set.
        now: Naive UTC instant the age is measured against; read once per
            request by the caller, ``utcnow()`` when omitted.
        exempt: Exempt tags (exact match); ``exempt_tags()`` when omitted. An
            empty collection adds no tags clause.

    Returns:
        The clauses, in the order they are written in the module docstring.
    """
    if now is None:
        now = utcnow()
    if exempt is None:
        exempt = exempt_tags()
    cutoff = now - timedelta(days=NEVER_DELIVERED_AGE_DAYS)
    out: list[ColumnElement[bool]] = [
        owned_by(Memory.user_id, viewer_user_id),
        Memory.created_at < cutoff,
        Memory.first_delivered_at.is_(None),
        and_(
            Memory.access_count == 0,
            Memory.reference_count == 0,
            Memory.last_used_at.is_(None),
        ),
        not_pinned_predicate(),
        Memory.type != MEMORY_TYPE_TIME,
        not_resource_owned_predicate(),
    ]
    if exempt:
        out.append(not_exempt_tagged_predicate(exempt))
    out.append(not_tool_triggered_predicate())
    # NULL-safe on ``memories.context_id`` (nullable): a correlated NOT EXISTS,
    # never NOT IN.
    out.append(
        not_(
            select(literal(1))
            .select_from(Context)
            .where(Context.id == Memory.context_id, Context.is_public.is_(True))
            .correlate(Memory)
            .exists()
        )
    )
    out.append(
        not_(
            select(literal(1))
            .select_from(NeuralMemoryEdge)
            .where(
                NeuralMemoryEdge.src_id == Memory.id,
                NeuralMemoryEdge.context_id == Memory.context_id,
                NeuralMemoryEdge.edge_type == EDGE_TYPE_SUPERSEDES,
            )
            .correlate(Memory)
            .exists()
        )
    )
    return out
