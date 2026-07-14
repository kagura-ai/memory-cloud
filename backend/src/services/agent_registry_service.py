"""Agent Registry service (RFC-0002 P0-1, Issue #1274).

Workspace-scoped CRUD over the ``agents`` table, shared by the REST routes
(``api/routes/agents.py``) and the MCP tools
(``mcp_server/tools/agent_registry.py``). Both surfaces are owner/admin-gated
by their own auth helpers; this service owns validation, duplicate checks,
the anti-abuse quota, the audit-lane rows, and the ``last_seen_at`` write
throttle.

Audit convention (design doc "Audit requirements for registry/binding CRUD"):
every mutation adds an ``audit_logs`` row via :func:`add_agent_audit_row`
mirroring ``member_api_key_provisioned``. The row is added to the session but
NOT committed — the caller commits it atomically with the mutation it audits.
``enforce`` → ``shadow`` is recorded under the distinct
``agent_enforcement_widened`` action: it silently widens every key bound to
the agent back to full member scope (privilege-widening, containment off).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.agent import (
    _ALL_AGENT_ENFORCEMENT_MODES,
    _ALL_AGENT_STATUSES,
    AGENT_ENFORCEMENT_ENFORCE,
    AGENT_ENFORCEMENT_SHADOW,
    Agent,
)
from utils.datetime import utcnow
from utils.exceptions import ConflictError, QuotaExceededError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Column-derived length caps, enforced here so overlong values return a
# structured 422 instead of a DB-layer 500.
_NAME_MAX_LEN = 255
_META_MAX_LEN = 100
_DESCRIPTION_MAX_LEN = 10_000

# Free-form metadata fields updatable without transition rules (open set,
# no CHECK — matches the DDL).
_MUTABLE_TEXT_FIELDS: tuple[str, ...] = ("description", "framework", "environment", "version")

# Audit action vocabulary (security-mutation lane).
AUDIT_AGENT_REGISTERED = "agent_registered"
AUDIT_AGENT_UPDATED = "agent_updated"
AUDIT_AGENT_ENFORCEMENT_WIDENED = "agent_enforcement_widened"
AUDIT_AGENT_DELETED = "agent_deleted"
# Issue #1276: an owner/admin operator bootstrapping an agent "on behalf of"
# (non-agent credential) — recorded so operator activity is distinguishable
# from the agent's own and cannot masquerade as it (F2 normative).
AUDIT_AGENT_BOOTSTRAP_ON_BEHALF = "agent_bootstrap_on_behalf_of"


def validate_agent_name(name: Any) -> str:
    """Validate and normalize an agent name. Returns the stripped name.

    Raises:
        ValidationError: If the name is not a non-empty string of at most
            255 characters after stripping.
    """
    if not isinstance(name, str):
        raise ValidationError("'name' must be a string", field="name")
    stripped = name.strip()
    if not stripped:
        raise ValidationError("'name' must be a non-empty string", field="name")
    if len(stripped) > _NAME_MAX_LEN:
        raise ValidationError(f"'name' must be at most {_NAME_MAX_LEN} characters", field="name")
    return stripped


def _validate_optional_text(value: Any, field: str, max_len: int) -> str | None:
    """Validate an optional free-form text field; None passes through."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"'{field}' must be a string", field=field)
    if len(value) > max_len:
        raise ValidationError(f"'{field}' must be at most {max_len} characters", field=field)
    return value


def validate_agent_status(value: Any) -> str:
    """Validate a ``status`` value against the CHECK tuple."""
    if value not in _ALL_AGENT_STATUSES:
        raise ValidationError(
            f"'status' must be one of {list(_ALL_AGENT_STATUSES)}", field="status"
        )
    return value


def validate_agent_enforcement_mode(value: Any) -> str:
    """Validate an ``enforcement_mode`` value against the CHECK tuple."""
    if value not in _ALL_AGENT_ENFORCEMENT_MODES:
        raise ValidationError(
            f"'enforcement_mode' must be one of {list(_ALL_AGENT_ENFORCEMENT_MODES)}",
            field="enforcement_mode",
        )
    return value


def add_agent_audit_row(
    db: AsyncSession,
    *,
    actor_user_id: str,
    actor_email: str | None,
    action: str,
    agent_id: uuid.UUID,
    workspace_id: uuid.UUID,
    metadata: dict[str, Any] | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
) -> None:
    """Add a security-mutation ``audit_logs`` row for a registry mutation.

    Mirrors ``audit_programmatic_workspace_action`` (#1164/#1165): the row is
    added to the session but NOT committed — the caller commits atomically
    with the audited mutation. ``old_value``/``new_value`` follow the
    established role-transition precedent (``auth/roles.py``): short
    non-sensitive enum values are stored raw in the hash columns.
    """
    from models.auth import AuditLog

    db.add(
        AuditLog(
            user_email=actor_email or f"{actor_user_id}@api",
            user_id=actor_user_id,
            action=action,
            resource=f"agent:{agent_id}",
            old_value_hash=old_value,
            new_value_hash=new_value,
            user_metadata={"workspace_id": str(workspace_id), **(metadata or {})},
        )
    )
    logger.info(
        "agent_registry_mutation",
        action=action,
        agent_id=str(agent_id),
        workspace_id=str(workspace_id),
        actor_id=actor_user_id,
    )


class AgentRegistryService:
    """CRUD + throttled liveness writes for the Agent Registry."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_agent(self, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        """Fetch one agent by id within the workspace boundary.

        The workspace filter is the IDOR guard: a caller can never address
        another workspace's agent even with a valid UUID.
        """
        result = await self.db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
        )
        return result.scalar_one_or_none()

    async def get_agent_by_name(self, workspace_id: uuid.UUID, name: str) -> Agent | None:
        """Fetch one agent by its workspace-unique name."""
        result = await self.db.execute(
            select(Agent).where(Agent.workspace_id == workspace_id, Agent.name == name)
        )
        return result.scalar_one_or_none()

    async def list_agents(self, workspace_id: uuid.UUID) -> list[Agent]:
        """List all agents in the workspace, newest first."""
        result = await self.db.execute(
            select(Agent)
            .where(Agent.workspace_id == workspace_id)
            .order_by(Agent.created_at.desc(), Agent.id)
        )
        return list(result.scalars().all())

    async def count_agents(self, workspace_id: uuid.UUID) -> int:
        """Count registry rows in the workspace (quota input)."""
        result = await self.db.execute(
            select(func.count()).select_from(Agent).where(Agent.workspace_id == workspace_id)
        )
        return int(result.scalar_one())

    async def create_agent(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        owner_user_id: str,
        description: str | None = None,
        framework: str | None = None,
        environment: str | None = None,
        version: str | None = None,
    ) -> Agent:
        """Register an agent following the setup_resource gate sequence.

        Gate order (after the surface's role check): validation → duplicate →
        plan gate → quota. Registry rows are cheap control-plane metadata, so
        no plan tier restricts them (the plan gate is a documented no-op); the
        quota is the anti-abuse ``max_agents_per_workspace`` settings cap.

        The row is flushed (PK assigned) but NOT committed — the caller
        commits atomically with its audit row.

        Raises:
            ValidationError: On malformed fields.
            ConflictError: If the name is already registered in the workspace.
            QuotaExceededError: If the workspace hit the registry cap.
        """
        clean_name = validate_agent_name(name)
        clean_description = _validate_optional_text(
            description, "description", _DESCRIPTION_MAX_LEN
        )
        clean_framework = _validate_optional_text(framework, "framework", _META_MAX_LEN)
        clean_environment = _validate_optional_text(environment, "environment", _META_MAX_LEN)
        clean_version = _validate_optional_text(version, "version", _META_MAX_LEN)

        if await self.get_agent_by_name(workspace_id, clean_name):
            raise ConflictError(
                f"Agent '{clean_name}' already exists in this workspace.",
            )

        # Plan gate: intentionally none — registry rows carry no embedding/LLM
        # cost and every plan may operate agents. Quota (anti-abuse cap) still
        # applies below.
        max_agents = get_settings().max_agents_per_workspace
        if await self.count_agents(workspace_id) >= max_agents:
            raise QuotaExceededError(
                f"Agent registry limit reached ({max_agents} per workspace).",
                quota_type="agents",
            )

        agent = Agent(
            workspace_id=workspace_id,
            name=clean_name,
            owner_user_id=owner_user_id,
            description=clean_description,
            framework=clean_framework,
            environment=clean_environment,
            version=clean_version,
        )
        self.db.add(agent)
        await self._flush_mapping_duplicate_name(clean_name)
        return agent

    async def update_agent(
        self, agent: Agent, updates: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Apply a validated partial update; returns ``{field: {old, new}}``.

        Recognized fields: ``name``, the free-form metadata fields, and the
        governed transitions ``status`` / ``enforcement_mode``. Unknown fields
        raise ValidationError so callers cannot silently write arbitrary
        columns. No-op assignments (same value) are dropped from the change
        set so audit rows record only real transitions.

        Changes are applied to the ORM row but NOT committed — the caller
        commits atomically with its audit row.
        """
        changes: dict[str, dict[str, Any]] = {}

        for field, raw in updates.items():
            if field == "name":
                new_name = validate_agent_name(raw)
                if new_name != agent.name:
                    existing = await self.get_agent_by_name(agent.workspace_id, new_name)
                    if existing and existing.id != agent.id:
                        raise ConflictError(
                            f"Agent '{new_name}' already exists in this workspace.",
                        )
                    changes["name"] = {"old": agent.name, "new": new_name}
                    agent.name = new_name
            elif field in _MUTABLE_TEXT_FIELDS:
                max_len = _DESCRIPTION_MAX_LEN if field == "description" else _META_MAX_LEN
                new_value = _validate_optional_text(raw, field, max_len)
                if new_value != getattr(agent, field):
                    changes[field] = {"old": getattr(agent, field), "new": new_value}
                    setattr(agent, field, new_value)
            elif field == "status":
                new_status = validate_agent_status(raw)
                if new_status != agent.status:
                    changes["status"] = {"old": agent.status, "new": new_status}
                    agent.status = new_status
            elif field == "enforcement_mode":
                new_mode = validate_agent_enforcement_mode(raw)
                if new_mode != agent.enforcement_mode:
                    changes["enforcement_mode"] = {
                        "old": agent.enforcement_mode,
                        "new": new_mode,
                    }
                    agent.enforcement_mode = new_mode
            else:
                raise ValidationError(f"Unknown agent field: '{field}'", field=field)

        if changes:
            await self._flush_mapping_duplicate_name(agent.name)
        return changes

    async def _flush_mapping_duplicate_name(self, name: str) -> None:
        """Flush, mapping a ``uq_agents_workspace_name`` race to ConflictError.

        The pre-insert duplicate SELECT and the flush are not atomic
        (TOCTOU): concurrent registration of the same name — e.g. replicas
        of one agent self-registering on startup — loses the race at flush
        with an IntegrityError that would otherwise surface as a misleading
        503 (REST) or a raw driver string (MCP). Same posture as
        ``setup_resource`` / ``external_keys``: recognize the constraint BY
        NAME via db.constraint_names and re-raise as the same 409 the
        pre-check produces; any other IntegrityError propagates unchanged.
        """
        from sqlalchemy.exc import IntegrityError

        from db.constraint_names import (
            AGENTS_WORKSPACE_NAME_UNIQUE,
            integrity_error_constraint_name,
        )

        try:
            await self.db.flush()
        except IntegrityError as exc:
            if integrity_error_constraint_name(exc) == AGENTS_WORKSPACE_NAME_UNIQUE:
                raise ConflictError(
                    f"Agent '{name}' already exists in this workspace.",
                ) from exc
            raise

    async def delete_agent(self, agent: Agent) -> None:
        """Hard-delete a registry row (admin operation).

        Operational retirement is ``status='retired'``, not row deletion —
        but hard delete stays available for admins; in P0-2 the
        ``api_keys.agent_id ON DELETE CASCADE`` makes it fail-closed (every
        bound key dies with the agent). NOT committed — the caller commits
        atomically with its audit row.
        """
        await self.db.delete(agent)
        await self.db.flush()

    async def touch_last_seen(self, agent_id: uuid.UUID) -> bool:
        """Throttled ``last_seen_at`` write (mirrors api_keys #947).

        At most one UPDATE per agent per
        ``agent_last_seen_throttle_seconds`` window, so hot correlation/verify
        paths (P0-2/P0-4) do not turn every request into a row UPDATE. Returns
        True when a write happened. Not committed — piggybacks on the caller's
        transaction.
        """
        now = utcnow().replace(tzinfo=None)
        throttle = timedelta(seconds=get_settings().agent_last_seen_throttle_seconds)
        result = await self.db.execute(
            update(Agent)
            .where(
                Agent.id == agent_id,
                or_(Agent.last_seen_at.is_(None), Agent.last_seen_at <= now - throttle),
            )
            .values(last_seen_at=now)
        )
        return bool(getattr(result, "rowcount", 0))


def enforcement_widened(changes: dict[str, dict[str, Any]]) -> bool:
    """True when the change set contains the audited ``enforce`` → ``shadow``
    privilege-widening transition."""
    mode_change = changes.get("enforcement_mode")
    return bool(
        mode_change
        and mode_change.get("old") == AGENT_ENFORCEMENT_ENFORCE
        and mode_change.get("new") == AGENT_ENFORCEMENT_SHADOW
    )
