"""REST routes for the agent session-state lane (Issue #906, follow-up to #889).

#889 shipped the ``set_state`` / ``get_state`` MCP tools + ``AgentStateService``
+ ``agent_states`` table. This module exposes the same lane over REST so non-MCP
/ dashboard consumers get the full set/get/list/delete surface.

Access control mirrors the MCP handler (``mcp_server/tools/state.py``) using the
REST-layer equivalents — every path resolves the context with a UNIFORM 404 first
so a cross-workspace context never leaks its existence via a 403 (CWE-639 / OWASP
A01), exactly like the MCP ``_resolve_context_for_read``:
- **reads** (get one / list) → ``PermissionService.resolve_context_for_workspace_read``
  (uniform 404 on unreachable/cross-workspace context).
- **writes** (set / delete) → ``resolve_context_for_workspace_read`` (uniform-404
  reach check) **then** ``check_context_write`` (editor/owner; a read-only viewer
  who CAN reach the context gets 403 — safe, since workspace membership is already
  confirmed so the 403 leaks nothing cross-workspace).

The agent_states lane is TTL-bounded and structurally excluded from ``recall()``
(separate table, never embedded). ``expires_at`` is not surfaced here because
``AgentStateService`` returns values, not rows — exposing remaining TTL is a
tracked follow-up, not in scope for this REST-wrapper issue.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser
from db.base import get_db
from services.agent_state_service import AgentStateService
from services.permission_service import PermissionService
from utils.exceptions import MemoryCloudException, NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/contexts", tags=["agent-state"])

# Mirrors the agent_states.key column (VARCHAR(255)) and the MCP handler's
# _STATE_KEY_MAX_LEN — keep the three in lock-step.
_STATE_KEY_MAX_LEN = 255


# === Schemas ===============================================================
class AgentStateSetRequest(BaseModel):
    """Body for upserting an agent-state entry.

    ``value`` is arbitrary JSON (stored in the JSONB column) but must not be
    null — the column is NOT NULL, and a null value is rejected up front as a
    422 rather than surfacing as a DB-layer 500.
    """

    value: Any = Field(..., description="Arbitrary JSON value to store (must not be null)")
    ttl_seconds: int | None = Field(
        None,
        gt=0,
        description=(
            "Optional TTL in seconds (clamped server-side to 30 days). "
            "Omit to persist until explicitly deleted."
        ),
    )


class AgentStateKeyResponse(BaseModel):
    """Envelope for a write that returns just the affected key (set / delete)."""

    key: str


class AgentStateValueResponse(BaseModel):
    """Envelope for a single-key read."""

    key: str
    value: Any


class AgentStateListResponse(BaseModel):
    """Envelope for listing all live entries in a context."""

    states: dict[str, Any]
    count: int


# === Dependency factories ==================================================
async def get_agent_state_service(db: AsyncSession = Depends(get_db)) -> AgentStateService:
    return AgentStateService(db)


async def get_permission_service(db: AsyncSession = Depends(get_db)) -> PermissionService:
    return PermissionService(db)


# === Routes ================================================================
@router.put("/{context_id}/state/{key}", response_model=AgentStateKeyResponse)
async def set_agent_state(
    context_id: UUID,
    user: APIKeyOrSessionUser,
    body: AgentStateSetRequest,
    key: str = Path(..., min_length=1, max_length=_STATE_KEY_MAX_LEN),
    service: AgentStateService = Depends(get_agent_state_service),
    perm: PermissionService = Depends(get_permission_service),
):
    """Upsert ``value`` at ``(context_id, key)`` (PUT = idempotent, 200 on replace)."""
    if body.value is None:
        # ValidationError (not a raw HTTPException) so the client gets the
        # standard {error, message, details} envelope + VAL-001 code via the
        # global MemoryCloudException handler.
        raise ValidationError("'value' must not be null", field="value")
    user_id = user.get("user_id")
    logger.info("agent_state_set_requested", user_id=user_id, context_id=str(context_id), key=key)
    try:
        # Reach check with UNIFORM 404 first (CWE-639: a cross-workspace context
        # must not leak its existence via a 403), then the editor/owner write
        # gate — whose 403 is safe once workspace membership is confirmed.
        await perm.resolve_context_for_workspace_read(
            user_id, context_id, key_workspace_id=user.get("api_key_workspace_id")
        )
        await perm.check_context_write(user_id, context_id)
        await service.set_state(context_id, key, body.value, ttl_seconds=body.ttl_seconds)
        return AgentStateKeyResponse(key=key)
    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error(
            "agent_state_set_failed", user_id=user_id, context_id=str(context_id), error=str(e)
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to set agent state",
        ) from e


@router.get("/{context_id}/state/{key}", response_model=AgentStateValueResponse)
async def get_agent_state(
    context_id: UUID,
    user: APIKeyOrSessionUser,
    key: str = Path(..., min_length=1, max_length=_STATE_KEY_MAX_LEN),
    service: AgentStateService = Depends(get_agent_state_service),
    perm: PermissionService = Depends(get_permission_service),
):
    """Read one key's live value. 404 when absent or expired."""
    user_id = user.get("user_id")
    logger.info("agent_state_get_requested", user_id=user_id, context_id=str(context_id), key=key)
    try:
        # Read gate with UNIFORM 404 on an unreachable/cross-workspace context
        # (CWE-639 IDOR guard; mirrors the MCP handler's _resolve_context_for_read).
        await perm.resolve_context_for_workspace_read(
            user_id, context_id, key_workspace_id=user.get("api_key_workspace_id")
        )
        value = await service.get_state(context_id, key)
        if value is None:
            raise NotFoundException("AgentState")
        return AgentStateValueResponse(key=key, value=value)
    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error(
            "agent_state_get_failed", user_id=user_id, context_id=str(context_id), error=str(e)
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get agent state",
        ) from e


@router.get("/{context_id}/state", response_model=AgentStateListResponse)
async def list_agent_state(
    context_id: UUID,
    user: APIKeyOrSessionUser,
    service: AgentStateService = Depends(get_agent_state_service),
    perm: PermissionService = Depends(get_permission_service),
):
    """List all live ``key → value`` entries for the context (200, empty = {})."""
    user_id = user.get("user_id")
    logger.info("agent_state_list_requested", user_id=user_id, context_id=str(context_id))
    try:
        # Uniform-404 read gate (CWE-639 IDOR guard).
        await perm.resolve_context_for_workspace_read(
            user_id, context_id, key_workspace_id=user.get("api_key_workspace_id")
        )
        states = await service.list_state(context_id)
        return AgentStateListResponse(states=states, count=len(states))
    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error(
            "agent_state_list_failed", user_id=user_id, context_id=str(context_id), error=str(e)
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list agent state",
        ) from e


@router.delete("/{context_id}/state/{key}", response_model=AgentStateKeyResponse)
async def delete_agent_state(
    context_id: UUID,
    user: APIKeyOrSessionUser,
    key: str = Path(..., min_length=1, max_length=_STATE_KEY_MAX_LEN),
    service: AgentStateService = Depends(get_agent_state_service),
    perm: PermissionService = Depends(get_permission_service),
):
    """Delete ``(context_id, key)``. 404 when the key was not present."""
    user_id = user.get("user_id")
    logger.info(
        "agent_state_delete_requested", user_id=user_id, context_id=str(context_id), key=key
    )
    try:
        # Uniform-404 reach check, then the editor/owner write gate (CWE-639).
        await perm.resolve_context_for_workspace_read(
            user_id, context_id, key_workspace_id=user.get("api_key_workspace_id")
        )
        await perm.check_context_write(user_id, context_id)
        removed = await service.delete_state(context_id, key)
        if not removed:
            raise NotFoundException("AgentState")
        return AgentStateKeyResponse(key=key)
    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error(
            "agent_state_delete_failed", user_id=user_id, context_id=str(context_id), error=str(e)
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete agent state",
        ) from e
