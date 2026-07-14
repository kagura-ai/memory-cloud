"""MCP tools for the Agent Registry (RFC-0002 P0-1, Issue #1274).

Owner/admin-gated CRUD over the workspace-scoped ``agents`` table, sharing
``services.agent_registry_service`` with the REST surface
(``api/routes/agents.py``). The gate sequence mirrors ``setup_resource``
(role → validation → duplicate → plan gate → quota); validation, duplicate,
and quota live in the service, the role gate lives here.

Every mutation writes an ``audit_logs`` row via the security-mutation lane
and commits it atomically with the mutation. ``enforce`` → ``shadow``
transitions are recorded under the distinct ``agent_enforcement_widened``
action.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import _error_response, _success_response

# Fields accepted by update_agent, forwarded verbatim to the service (which
# validates values and rejects unknown fields).
_UPDATABLE_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "framework",
    "environment",
    "version",
    "status",
    "enforcement_mode",
)


def _parse_agent_id(raw: Any) -> UUID | None:
    """Parse an ``agent_id`` argument; None when malformed."""
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _serialize_agent(agent: Any) -> dict[str, Any]:
    """Registry row → JSON-safe dict (Z-suffixed UTC timestamps)."""
    from utils.datetime import to_utc_iso

    return {
        "id": str(agent.id),
        "workspace_id": str(agent.workspace_id),
        "name": agent.name,
        "description": agent.description,
        "owner_user_id": agent.owner_user_id,
        "framework": agent.framework,
        "environment": agent.environment,
        "version": agent.version,
        "status": agent.status,
        "enforcement_mode": agent.enforcement_mode,
        "last_seen_at": to_utc_iso(agent.last_seen_at),
        "created_at": to_utc_iso(agent.created_at),
        "updated_at": to_utc_iso(agent.updated_at),
    }


async def handle_register_agent(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Register an agent in the active workspace (owner/admin only)."""
    if "name" not in args:
        return _error_response("missing_fields", "Missing required field: name")
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    from db.base import get_db
    from mcp_server.tools.resource import _check_owner_admin_role
    from services.agent_registry_service import (
        AUDIT_AGENT_REGISTERED,
        AgentRegistryService,
        add_agent_audit_row,
    )
    from utils.exceptions import ConflictError, QuotaExceededError, ValidationError

    async for db in get_db():
        role_err = await _check_owner_admin_role(db, user_id, workspace_id)
        if role_err:
            return role_err

        service = AgentRegistryService(db)
        try:
            agent = await service.create_agent(
                workspace_id=workspace_id,
                name=args["name"],
                owner_user_id=user_id,
                description=args.get("description"),
                framework=args.get("framework"),
                environment=args.get("environment"),
                version=args.get("version"),
            )
        except ValidationError as e:
            return _error_response("validation_error", e.message)
        except ConflictError as e:
            return _error_response("agent_name_conflict", e.message)
        except QuotaExceededError as e:
            return _error_response("quota_exceeded", e.message)

        add_agent_audit_row(
            db,
            actor_user_id=user_id,
            actor_email=None,
            action=AUDIT_AGENT_REGISTERED,
            agent_id=agent.id,
            workspace_id=workspace_id,
            metadata={"via": "mcp", "agent_name": agent.name},
        )
        await db.commit()
        await db.refresh(agent)
        return _success_response(agent=_serialize_agent(agent))

    return _error_response("internal_error", "Database session unavailable")


async def handle_list_agents(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List the active workspace's registered agents (owner/admin only)."""
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    from db.base import get_db
    from mcp_server.tools.resource import _check_owner_admin_role
    from services.agent_registry_service import AgentRegistryService

    async for db in get_db():
        role_err = await _check_owner_admin_role(db, user_id, workspace_id)
        if role_err:
            return role_err

        agents = await AgentRegistryService(db).list_agents(workspace_id)
        return _success_response(agents=[_serialize_agent(a) for a in agents], count=len(agents))

    return _error_response("internal_error", "Database session unavailable")


async def handle_get_agent(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Fetch one registered agent by id (owner/admin only)."""
    if "agent_id" not in args:
        return _error_response("missing_fields", "Missing required field: agent_id")
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")
    agent_id = _parse_agent_id(args["agent_id"])
    if agent_id is None:
        return _error_response("validation_error", "agent_id must be a UUID.")

    from db.base import get_db
    from mcp_server.tools.resource import _check_owner_admin_role
    from services.agent_registry_service import AgentRegistryService

    async for db in get_db():
        role_err = await _check_owner_admin_role(db, user_id, workspace_id)
        if role_err:
            return role_err

        agent = await AgentRegistryService(db).get_agent(workspace_id, agent_id)
        if agent is None:
            return _error_response("agent_not_found", "Agent not found in your workspace.")
        return _success_response(agent=_serialize_agent(agent))

    return _error_response("internal_error", "Database session unavailable")


async def handle_update_agent(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Partially update an agent, including status / enforcement transitions."""
    if "agent_id" not in args:
        return _error_response("missing_fields", "Missing required field: agent_id")
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")
    agent_id = _parse_agent_id(args["agent_id"])
    if agent_id is None:
        return _error_response("validation_error", "agent_id must be a UUID.")

    updates = {k: args[k] for k in _UPDATABLE_FIELDS if k in args}
    if not updates:
        return _error_response(
            "missing_fields",
            f"Provide at least one updatable field: {', '.join(_UPDATABLE_FIELDS)}",
        )

    from db.base import get_db
    from mcp_server.tools.resource import _check_owner_admin_role
    from services.agent_registry_service import (
        AUDIT_AGENT_ENFORCEMENT_WIDENED,
        AUDIT_AGENT_UPDATED,
        AgentRegistryService,
        add_agent_audit_row,
        enforcement_widened,
    )
    from utils.exceptions import ConflictError, ValidationError

    async for db in get_db():
        role_err = await _check_owner_admin_role(db, user_id, workspace_id)
        if role_err:
            return role_err

        service = AgentRegistryService(db)
        agent = await service.get_agent(workspace_id, agent_id)
        if agent is None:
            return _error_response("agent_not_found", "Agent not found in your workspace.")

        try:
            changes = await service.update_agent(agent, updates)
        except ValidationError as e:
            return _error_response("validation_error", e.message)
        except ConflictError as e:
            return _error_response("agent_name_conflict", e.message)

        if changes:
            widened = enforcement_widened(changes)
            transition = changes.get("enforcement_mode") or changes.get("status")
            add_agent_audit_row(
                db,
                actor_user_id=user_id,
                actor_email=None,
                action=AUDIT_AGENT_ENFORCEMENT_WIDENED if widened else AUDIT_AGENT_UPDATED,
                agent_id=agent.id,
                workspace_id=workspace_id,
                metadata={"via": "mcp", "agent_name": agent.name, "changes": changes},
                old_value=transition.get("old") if transition else None,
                new_value=transition.get("new") if transition else None,
            )
            await db.commit()
            await db.refresh(agent)
        return _success_response(agent=_serialize_agent(agent), changed=sorted(changes))

    return _error_response("internal_error", "Database session unavailable")


async def handle_delete_agent(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Hard-delete a registry row (owner/admin only).

    Operational retirement should normally use ``status='retired'``; from
    P0-2 on, hard delete cascades every key bound to the agent (fail-closed).
    """
    if "agent_id" not in args:
        return _error_response("missing_fields", "Missing required field: agent_id")
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")
    agent_id = _parse_agent_id(args["agent_id"])
    if agent_id is None:
        return _error_response("validation_error", "agent_id must be a UUID.")

    from db.base import get_db
    from mcp_server.tools.resource import _check_owner_admin_role
    from services.agent_registry_service import (
        AUDIT_AGENT_DELETED,
        AgentRegistryService,
        add_agent_audit_row,
    )

    async for db in get_db():
        role_err = await _check_owner_admin_role(db, user_id, workspace_id)
        if role_err:
            return role_err

        service = AgentRegistryService(db)
        agent = await service.get_agent(workspace_id, agent_id)
        if agent is None:
            return _error_response("agent_not_found", "Agent not found in your workspace.")

        deleted_id: uuid.UUID = agent.id
        add_agent_audit_row(
            db,
            actor_user_id=user_id,
            actor_email=None,
            action=AUDIT_AGENT_DELETED,
            agent_id=deleted_id,
            workspace_id=workspace_id,
            metadata={"via": "mcp", "agent_name": agent.name},
        )
        await service.delete_agent(agent)
        await db.commit()
        return _success_response(deleted=True, agent_id=str(deleted_id))

    return _error_response("internal_error", "Database session unavailable")
