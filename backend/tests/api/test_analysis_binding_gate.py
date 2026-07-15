"""Issue #1281 item 5: subtractive agent-binding gate on the analysis surfaces.

Both the MCP helper (`_verify_context_in_workspace_mcp`) and the REST helper
(`_verify_context_in_workspace`) apply `agent_binding_permits` after the
workspace-boundary check, denying with the same uniform not-found shape when an
agent-bound credential's binding does not permit the target context. No-op for
non-agent credentials (agent_binding_permits returns True).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

WS = uuid.uuid4()
CTX = uuid.uuid4()

_WS_OK = "services.analysis.query_service.verify_context_in_workspace"
_BINDING = "services.agent_binding_service.agent_binding_permits"


@pytest.mark.asyncio
async def test_mcp_gate_denies_when_binding_rejects():
    from mcp_server.tools.analysis import _verify_context_in_workspace_mcp

    with (
        patch(_WS_OK, new=AsyncMock(return_value=True)),
        patch(_BINDING, new=AsyncMock(return_value=False)),
    ):
        result = await _verify_context_in_workspace_mcp(
            MagicMock(), workspace_id=WS, context_id=CTX
        )
    # Non-None = an error envelope (context_not_found), not a pass-through.
    assert result is not None


@pytest.mark.asyncio
async def test_mcp_gate_passes_when_binding_permits():
    from mcp_server.tools.analysis import _verify_context_in_workspace_mcp

    with (
        patch(_WS_OK, new=AsyncMock(return_value=True)),
        patch(_BINDING, new=AsyncMock(return_value=True)),
    ):
        result = await _verify_context_in_workspace_mcp(
            MagicMock(), workspace_id=WS, context_id=CTX
        )
    assert result is None


@pytest.mark.asyncio
async def test_rest_gate_denies_when_binding_rejects():
    from api.routes.analyses import _verify_context_in_workspace

    with (
        patch(_WS_OK, new=AsyncMock(return_value=True)),
        patch(_BINDING, new=AsyncMock(return_value=False)),
        pytest.raises(HTTPException) as exc,
    ):
        await _verify_context_in_workspace(MagicMock(), workspace_id=WS, context_id=CTX)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_rest_gate_passes_when_binding_permits():
    from api.routes.analyses import _verify_context_in_workspace

    with (
        patch(_WS_OK, new=AsyncMock(return_value=True)),
        patch(_BINDING, new=AsyncMock(return_value=True)),
    ):
        # No raise = pass.
        await _verify_context_in_workspace(MagicMock(), workspace_id=WS, context_id=CTX)
