"""Per-request agent scope for binding enforcement (RFC-0002 P0-2, #1275).

When a request authenticates with an agent-bound member API key
(``api_keys.agent_id`` set), the verified agent identity and its
``enforcement_mode`` are recorded here so the context-resolution chokepoints
(``PermissionService.resolve_context_for_workspace_read`` /
``check_context_access`` / ``get_accessible_contexts`` and the MCP
``_resolve_context`` write path) can apply the **purely subtractive** binding
intersection without threading a parameter through every call site — the
same per-request contextvar pattern as the #963 MCP key-workspace scope.

The scope is ``None`` for every non-agent authentication (session, OAuth,
global/workspace member keys without ``agent_id``), which makes the binding
filter a structural no-op there — the backward-compat matrix's
"byte-for-byte unchanged" guarantee. Fail-open is impossible by construction:
a set scope can only *remove* access the underlying RBAC decision granted.

ContextVar isolation: each ASGI request runs in its own task context, so a
scope set for one request can never leak into another; the auth adapters
(REST dependencies, MCP transport) also reset it explicitly per request as
defense in depth.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class AgentScope:
    """Verified agent identity attached to the current request's credential."""

    agent_id: UUID
    enforcement_mode: str


_agent_scope: ContextVar[AgentScope | None] = ContextVar("agent_scope", default=None)


def set_agent_scope(scope: AgentScope | None) -> None:
    """Set (or clear) the current request's agent scope."""
    _agent_scope.set(scope)


def get_agent_scope() -> AgentScope | None:
    """Read the current request's agent scope (None = not an agent credential)."""
    return _agent_scope.get()


def set_agent_scope_from_verified(verified: Any) -> None:
    """Set the scope from a ``VerifiedKey``-shaped object (or clear it).

    Accepts anything exposing ``agent_id`` / ``agent_enforcement_mode`` so the
    auth adapters do not need to import ``auth.api_keys`` (and duck-typing
    keeps test doubles cheap). A verified key without an agent binding clears
    the scope.
    """
    agent_id = getattr(verified, "agent_id", None)
    mode = getattr(verified, "agent_enforcement_mode", None)
    if verified is None or agent_id is None:
        # Not an agent-bound credential — clear any stale scope.
        set_agent_scope(None)
        return
    if mode is None:
        # An agent-bound key (agent_id set) with no enforcement_mode is an
        # anomaly — verify_key only sets agent_id for an ACTIVE agent whose
        # mode is a NOT-NULL/CHECK-constrained column, so this can only arise
        # from a bug, partial select, or test double. Fail CLOSED: default to
        # ``enforce``, which with no bindings is default-deny — never silently
        # widen the key to full member scope (code-review hardening).
        from models.agent import AGENT_ENFORCEMENT_ENFORCE

        mode = AGENT_ENFORCEMENT_ENFORCE
    set_agent_scope(AgentScope(agent_id=agent_id, enforcement_mode=mode))
