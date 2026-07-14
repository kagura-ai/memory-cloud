"""Agent Registry (RFC-0002 P0-1, Issue #1274).

Workspace-scoped registry of AI agents. An agent is a **resource, not a
principal** (design invariant, ``docs/design/agent-registry-and-bindings.md``):
it never authenticates by itself — enforcement attaches to member API keys
that gain a nullable ``agent_id`` pointer in P0-2. This table is the anchor
every other RFC-0002 P0 item builds on (bindings, bootstrap, correlation,
access events).

``status`` is the fail-closed kill switch: ``suspended``/``retired`` agents
cause every key bound to them to be rejected at verify time (one row update
beats revoking N keys). ``enforcement_mode='shadow'`` records binding
violations as would-deny audit rows while requests proceed under legacy
semantics — the migration ramp for binding existing, in-use keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Agent lifecycle kill switch. CHECK constraint is derived from this tuple
# (registration order, single quotes) byte-identically to the alembic
# migration literal — the ``valid_delivery_mode`` drift-pin pattern (#886).
# New values are APPENDED, never reordered; drift is pinned by
# tests/test_agent_constants.py.
AGENT_STATUS_ACTIVE = "active"
AGENT_STATUS_SUSPENDED = "suspended"
AGENT_STATUS_RETIRED = "retired"
_ALL_AGENT_STATUSES: tuple[str, ...] = (
    AGENT_STATUS_ACTIVE,
    AGENT_STATUS_SUSPENDED,
    AGENT_STATUS_RETIRED,
)

# Enforcement ramp for P0-2 bindings. ``enforce`` → ``shadow`` is an audited
# privilege-widening transition (it silently widens every key bound to the
# agent back to full member scope).
AGENT_ENFORCEMENT_SHADOW = "shadow"
AGENT_ENFORCEMENT_ENFORCE = "enforce"
_ALL_AGENT_ENFORCEMENT_MODES: tuple[str, ...] = (
    AGENT_ENFORCEMENT_SHADOW,
    AGENT_ENFORCEMENT_ENFORCE,
)

# Binding write policy (P0-2, #1275). ``'staged'`` is reserved for P1 — CHECK
# tuples are append-only by house convention, so adding it later is a cheap
# migration. Same drift-pin discipline as the agent tuples above.
BINDING_WRITE_POLICY_DENY = "deny"
BINDING_WRITE_POLICY_DIRECT = "direct"
_ALL_BINDING_WRITE_POLICIES: tuple[str, ...] = (
    BINDING_WRITE_POLICY_DENY,
    BINDING_WRITE_POLICY_DIRECT,
)


class Agent(Base):
    """One row per registered agent, unique by ``(workspace_id, name)``.

    ``id`` doubles as the OTel ``gen_ai.agent.id`` correlation anchor (P0-4);
    ``name`` maps to ``gen_ai.agent.name``. ``framework`` / ``environment`` /
    ``version`` are free-form client-reported metadata (open set, no CHECK).
    """

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # OAuth sub; plain string per repo convention (cf. workspaces.owner_user_id).
    # Pseudonymized by account_erasure_service for erased subjects whose rows
    # survive (agents in co-owned workspaces that are not hard-deleted).
    owner_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    framework: Mapped[str | None] = mapped_column(String(100), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AGENT_STATUS_ACTIVE,
        server_default=text("'active'"),
    )
    enforcement_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AGENT_ENFORCEMENT_ENFORCE,
        server_default=text("'enforce'"),
    )
    # Write-throttled like api_keys.last_used_at (#947) — see
    # AgentRegistryService.touch_last_seen. Naive UTC by convention.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # CHECKs derived from the module-level tuples (registration order,
        # single quotes, exact whitespace), byte-identical to the alembic
        # migration literal so create_all and the migration head never drift.
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _ALL_AGENT_STATUSES)})",
            name="valid_agent_status",
        ),
        CheckConstraint(
            f"enforcement_mode IN ({', '.join(repr(m) for m in _ALL_AGENT_ENFORCEMENT_MODES)})",
            name="valid_agent_enforcement",
        ),
        Index("uq_agents_workspace_name", "workspace_id", "name", unique=True),
        Index("idx_agents_workspace", "workspace_id"),
    )


class AgentContextBinding(Base):
    """Purely subtractive per-context scoping for one agent (P0-2, #1275).

    The effective permission for an agent-bound request is *existing
    PermissionService decision ∩ binding* — a binding can never grant access
    the underlying member row does not have (design invariant 2,
    ``docs/design/agent-registry-and-bindings.md``). The agent's **read set**
    is the rows with ``can_read = true``; its **write set** is the rows with
    ``write_policy = 'direct'``. No binding row for a context = deny under
    ``enforce`` (default-deny applies only to newly bound agents).

    NULL/[] array semantics are fixed: ``NULL`` = unrestricted, ``[]`` =
    deny-all — deliberately NOT reproducing the role-dependent
    ``allowed_context_ids`` NULL asymmetry. ``is_default`` is the single
    source of truth for the bootstrap default binding (at most one per agent,
    guaranteed by the partial unique index).
    """

    __tablename__ = "agent_context_bindings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
    )
    can_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    write_policy: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BINDING_WRITE_POLICY_DENY,
        server_default=text("'deny'"),
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # NULL = all types allowed; [] = none. Memory types are an open vocabulary
    # (no CHECK); source types take values from the memories.source_type set,
    # validated at the service layer.
    allowed_memory_types: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True)
    allowed_source_types: Mapped[list[str] | None] = mapped_column(ARRAY(String(20)), nullable=True)
    # OAuth sub of the binding author; pseudonymized by account_erasure_service
    # for erased subjects whose rows survive.
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"write_policy IN ({', '.join(repr(p) for p in _ALL_BINDING_WRITE_POLICIES)})",
            name="valid_binding_write_policy",
        ),
        Index("uq_agent_ctx_binding", "agent_id", "context_id", unique=True),
        # At most one bootstrap default binding per agent.
        Index(
            "uq_agent_ctx_binding_default",
            "agent_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        Index("idx_agent_ctx_binding_context", "context_id"),
    )
