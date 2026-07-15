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
        AgentRegistryService,
        add_agent_update_audit_rows,
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
            # #1294: one audit row per governed transition (parity with the REST
            # surface) — a combined status+enforcement PATCH must not collapse.
            add_agent_update_audit_rows(
                db,
                actor_user_id=user_id,
                actor_email=None,
                agent_id=agent.id,
                agent_name=agent.name,
                workspace_id=workspace_id,
                changes=changes,
                extra_metadata={"via": "mcp"},
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


# ============================================================================
# Context bindings (RFC-0002 P0-2, Issue #1275)
# ============================================================================

# Binding fields accepted by bind/update, forwarded to the service (which
# validates values and rejects unknown fields).
_BINDING_UPDATABLE_FIELDS: tuple[str, ...] = (
    "can_read",
    "write_policy",
    "is_default",
    "allowed_memory_types",
    "allowed_source_types",
)


def _serialize_binding(binding: Any) -> dict[str, Any]:
    """Binding row → JSON-safe dict (Z-suffixed UTC timestamps)."""
    from utils.datetime import to_utc_iso

    return {
        "id": str(binding.id),
        "agent_id": str(binding.agent_id),
        "context_id": str(binding.context_id),
        "can_read": binding.can_read,
        "write_policy": binding.write_policy,
        "is_default": binding.is_default,
        "allowed_memory_types": binding.allowed_memory_types,
        "allowed_source_types": binding.allowed_source_types,
        "created_by": binding.created_by,
        "created_at": to_utc_iso(binding.created_at),
        "updated_at": to_utc_iso(binding.updated_at),
    }


async def _resolve_agent_for_binding_op(db: Any, user_id: str, workspace_id: UUID, args: dict):
    """Shared preamble: role gate + agent lookup. Returns (agent, error)."""
    from mcp_server.tools.resource import _check_owner_admin_role
    from services.agent_registry_service import AgentRegistryService

    role_err = await _check_owner_admin_role(db, user_id, workspace_id)
    if role_err:
        return None, role_err
    agent_id = _parse_agent_id(args["agent_id"])
    if agent_id is None:
        return None, _error_response("validation_error", "agent_id must be a UUID.")
    agent = await AgentRegistryService(db).get_agent(workspace_id, agent_id)
    if agent is None:
        return None, _error_response("agent_not_found", "Agent not found in your workspace.")
    return agent, None


async def handle_bind_agent_context(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Bind an agent to a context (owner/admin only, purely subtractive)."""
    for field in ("agent_id", "context_id"):
        if field not in args:
            return _error_response("missing_fields", f"Missing required field: {field}")
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")
    context_id = _parse_agent_id(args["context_id"])
    if context_id is None:
        return _error_response("validation_error", "context_id must be a UUID.")

    from db.base import get_db
    from services.agent_binding_service import (
        AUDIT_BINDING_CREATED,
        AgentBindingService,
    )
    from services.agent_registry_service import add_agent_audit_row
    from utils.exceptions import ConflictError, NotFoundException, ValidationError

    async for db in get_db():
        agent, err = await _resolve_agent_for_binding_op(db, user_id, workspace_id, args)
        if err:
            return err

        try:
            binding = await AgentBindingService(db).create_binding(
                agent=agent,
                context_id=context_id,
                created_by=user_id,
                can_read=args.get("can_read", True),
                write_policy=args.get("write_policy", "deny"),
                is_default=args.get("is_default", False),
                allowed_memory_types=args.get("allowed_memory_types"),
                allowed_source_types=args.get("allowed_source_types"),
            )
        except ValidationError as e:
            return _error_response("validation_error", e.message)
        except NotFoundException:
            return _error_response(
                "context_not_found", "Context not found in the agent's workspace."
            )
        except ConflictError as e:
            return _error_response("binding_conflict", e.message)

        add_agent_audit_row(
            db,
            actor_user_id=user_id,
            actor_email=None,
            action=AUDIT_BINDING_CREATED,
            agent_id=agent.id,
            workspace_id=workspace_id,
            metadata={
                "via": "mcp",
                "agent_name": agent.name,
                "context_id": str(context_id),
                "binding_id": str(binding.id),
                "can_read": binding.can_read,
                "write_policy": binding.write_policy,
                "is_default": binding.is_default,
                # #1299: the filter arrays are behavior-bearing now — audit
                # them at create (update audits them via the changes dict).
                "allowed_memory_types": binding.allowed_memory_types,
                "allowed_source_types": binding.allowed_source_types,
            },
        )
        await db.commit()
        await db.refresh(binding)
        return _success_response(binding=_serialize_binding(binding))

    return _error_response("internal_error", "Database session unavailable")


async def handle_list_agent_bindings(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List an agent's context bindings (owner/admin only)."""
    if "agent_id" not in args:
        return _error_response("missing_fields", "Missing required field: agent_id")
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    from db.base import get_db
    from services.agent_binding_service import AgentBindingService

    async for db in get_db():
        agent, err = await _resolve_agent_for_binding_op(db, user_id, workspace_id, args)
        if err:
            return err
        bindings = await AgentBindingService(db).list_bindings(agent)
        return _success_response(
            bindings=[_serialize_binding(b) for b in bindings], count=len(bindings)
        )

    return _error_response("internal_error", "Database session unavailable")


async def handle_update_agent_binding(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Partially update a binding (owner/admin only)."""
    for field in ("agent_id", "binding_id"):
        if field not in args:
            return _error_response("missing_fields", f"Missing required field: {field}")
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")
    binding_id = _parse_agent_id(args["binding_id"])
    if binding_id is None:
        return _error_response("validation_error", "binding_id must be a UUID.")

    updates = {k: args[k] for k in _BINDING_UPDATABLE_FIELDS if k in args}
    if not updates:
        return _error_response(
            "missing_fields",
            f"Provide at least one updatable field: {', '.join(_BINDING_UPDATABLE_FIELDS)}",
        )

    from db.base import get_db
    from services.agent_binding_service import (
        AUDIT_BINDING_UPDATED,
        AgentBindingService,
    )
    from services.agent_registry_service import add_agent_audit_row
    from utils.exceptions import ConflictError, ValidationError

    async for db in get_db():
        agent, err = await _resolve_agent_for_binding_op(db, user_id, workspace_id, args)
        if err:
            return err
        service = AgentBindingService(db)
        binding = await service.get_binding(agent, binding_id)
        if binding is None:
            return _error_response("binding_not_found", "Binding not found for this agent.")

        try:
            changes = await service.update_binding(binding, updates)
        except ValidationError as e:
            return _error_response("validation_error", e.message)
        except ConflictError as e:
            return _error_response("binding_conflict", e.message)

        if changes:
            add_agent_audit_row(
                db,
                actor_user_id=user_id,
                actor_email=None,
                action=AUDIT_BINDING_UPDATED,
                agent_id=agent.id,
                workspace_id=workspace_id,
                metadata={
                    "via": "mcp",
                    "agent_name": agent.name,
                    "context_id": str(binding.context_id),
                    "binding_id": str(binding.id),
                    "changes": changes,
                },
            )
            await db.commit()
            await db.refresh(binding)
        return _success_response(binding=_serialize_binding(binding), changed=sorted(changes))

    return _error_response("internal_error", "Database session unavailable")


async def handle_unbind_agent_context(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Delete a binding (owner/admin only) — the agent loses that context."""
    for field in ("agent_id", "binding_id"):
        if field not in args:
            return _error_response("missing_fields", f"Missing required field: {field}")
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")
    binding_id = _parse_agent_id(args["binding_id"])
    if binding_id is None:
        return _error_response("validation_error", "binding_id must be a UUID.")

    from db.base import get_db
    from services.agent_binding_service import (
        AUDIT_BINDING_DELETED,
        AgentBindingService,
    )
    from services.agent_registry_service import add_agent_audit_row

    async for db in get_db():
        agent, err = await _resolve_agent_for_binding_op(db, user_id, workspace_id, args)
        if err:
            return err
        service = AgentBindingService(db)
        binding = await service.get_binding(agent, binding_id)
        if binding is None:
            return _error_response("binding_not_found", "Binding not found for this agent.")

        deleted_id = binding.id
        add_agent_audit_row(
            db,
            actor_user_id=user_id,
            actor_email=None,
            action=AUDIT_BINDING_DELETED,
            agent_id=agent.id,
            workspace_id=workspace_id,
            metadata={
                "via": "mcp",
                "agent_name": agent.name,
                "context_id": str(binding.context_id),
                "binding_id": str(deleted_id),
            },
        )
        await service.delete_binding(binding)
        await db.commit()
        return _success_response(deleted=True, binding_id=str(deleted_id))

    return _error_response("internal_error", "Database session unavailable")
