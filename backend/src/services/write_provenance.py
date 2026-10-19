"""Who wrote a memory: the writing agent and session (#2044).

Every memory write (remember, remember_batch, update_memory / PATCH, forget,
forget_bulk, rollback_session) records the caller's verified agent and
session on the ``memory_revisions`` row it adds, and as the memory's last
writer on ``memories.agent_id`` / ``memories.session_id``.

- ``agent_id`` comes only from the verified credential (``AgentScope``, set
  by the auth adapters for an agent-bound key). A baggage claim never lands
  here — it is attribution-only (``api/correlation.py``).
- ``session_id`` comes from the request's correlation context. Precedence
  (#2122): the OTel baggage session (``gen_ai.conversation.id`` /
  ``session.id``) > a client-supplied id on an MCP request —
  ``params._meta['ai.kagura/sessionId']`` (one tools/call) over the
  ``Kagura-Session-Id`` header (the whole request) > on the stateful MCP
  transport, the protocol session id the transport fills in. Every source
  passes the same token rule (``api.correlation``).

A session is an attribute, not an identity — whichever source named it, a
client can pick any id: it is only ever addressed
together with its principal — the caller sub and the credential's agent
(``SessionPrincipal``) — so naming another principal's session id selects
nothing (``principal_predicate``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy.sql import ColumnElement


@dataclass(frozen=True)
class WriteProvenance:
    """The verified agent and the session of one write (both optional)."""

    agent_id: UUID | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class SessionPrincipal:
    """Whose session: the caller sub and the credential's verified agent.

    ``agent_id`` is None for a user credential (session, OAuth, a member key
    without an agent) — that principal's sessions are the ones written with
    no agent, never an agent's.
    """

    actor_user_id: str
    agent_id: UUID | None


def current_provenance() -> WriteProvenance:
    """The current request's write provenance (empty outside a request)."""
    from api.correlation import get_correlation
    from auth.agent_scope import get_agent_scope

    scope = get_agent_scope()
    corr = get_correlation()
    return WriteProvenance(
        agent_id=scope.agent_id if scope is not None else None,
        # Validated when the correlation was built (charset, ≤ 128 chars),
        # which is the column's width.
        session_id=corr.session_id if corr is not None else None,
    )


def caller_principal(user_id: str) -> SessionPrincipal:
    """The principal whose sessions this caller may address."""
    from auth.agent_scope import get_agent_scope

    scope = get_agent_scope()
    return SessionPrincipal(
        actor_user_id=user_id, agent_id=scope.agent_id if scope is not None else None
    )


def principal_predicate(
    actor_column: Any, agent_column: Any, principal: SessionPrincipal
) -> ColumnElement[bool]:
    """``actor = sub AND agent IS NOT DISTINCT FROM agent_id``, index-friendly.

    Spelled as ``IS NULL`` / ``=`` rather than ``IS NOT DISTINCT FROM`` so the
    planner can use the column's index.
    """
    agent = (
        agent_column.is_(None) if principal.agent_id is None else agent_column == principal.agent_id
    )
    return and_(actor_column == principal.actor_user_id, agent)
