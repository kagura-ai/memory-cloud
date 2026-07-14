"""Route-surface tests for the agent bootstrap REST companion (Issue #1276).

Pins the identity/authorization → HTTP mapping (uniform 404 for
agent_not_found / context_not_found; 400 for context_id_required) and the
success envelope pass-through. Composition is covered by the service tests.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes.agents import BootstrapRequest, agent_bootstrap
from utils.exceptions import BadRequestError, NotFoundException

WORKSPACE_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()
CONTEXT_ID = uuid.uuid4()
USER = {"user_id": "u", "current_workspace_id": WORKSPACE_ID, "api_key_workspace_id": WORKSPACE_ID}


@pytest.fixture
def db():
    d = MagicMock()
    d.commit = AsyncMock()
    d.rollback = AsyncMock()
    return d


def _svc(monkeypatch, *, principal_error=None, context_error=None, envelope=None):
    from services.agent_bootstrap_service import BootstrapError

    inst = MagicMock()
    if principal_error:
        inst.resolve_principal_and_agent = AsyncMock(side_effect=BootstrapError(*principal_error))
    else:
        inst.resolve_principal_and_agent = AsyncMock(
            return_value=(
                SimpleNamespace(workspace_id=WORKSPACE_ID),
                SimpleNamespace(id=AGENT_ID, name="ci-bot", workspace_id=WORKSPACE_ID),
            )
        )
    if context_error:
        inst.resolve_context = AsyncMock(side_effect=BootstrapError(*context_error))
    else:
        inst.resolve_context = AsyncMock(
            return_value=(
                SimpleNamespace(id=CONTEXT_ID, workspace_id=WORKSPACE_ID),
                {"context_id": str(CONTEXT_ID), "is_default": True},
            )
        )
    inst.build_envelope = AsyncMock(
        return_value=envelope or {"status": "success", "degraded": False, "components": {}}
    )
    inst.audit_on_behalf_of = AsyncMock()
    monkeypatch.setattr(
        "services.agent_bootstrap_service.AgentBootstrapService",
        MagicMock(return_value=inst),
    )
    return inst


@pytest.mark.asyncio
async def test_success_returns_envelope(db, monkeypatch):
    _svc(monkeypatch)
    result = await agent_bootstrap(agent_id=AGENT_ID, body=BootstrapRequest(), user=USER, db=db)
    assert result["status"] == "success"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_not_found_is_404(db, monkeypatch):
    _svc(monkeypatch, principal_error=("agent_not_found", "Agent not found."))
    with pytest.raises(NotFoundException):
        await agent_bootstrap(agent_id=AGENT_ID, body=BootstrapRequest(), user=USER, db=db)
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_context_not_found_is_404(db, monkeypatch):
    _svc(monkeypatch, context_error=("context_not_found", "Context not found."))
    with pytest.raises(NotFoundException):
        await agent_bootstrap(agent_id=AGENT_ID, body=BootstrapRequest(), user=USER, db=db)


@pytest.mark.asyncio
async def test_context_id_required_is_400(db, monkeypatch):
    _svc(monkeypatch, context_error=("context_id_required", "context_id is required."))
    with pytest.raises(BadRequestError):
        await agent_bootstrap(agent_id=AGENT_ID, body=BootstrapRequest(), user=USER, db=db)
