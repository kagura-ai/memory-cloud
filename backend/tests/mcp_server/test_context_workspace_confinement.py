"""MCP key-workspace confinement via the per-request contextvar (Issue #963).

The MCP transport conflates ``workspace_id`` (api_key_workspace_id for a
workspace-scoped key, else the user's *current* workspace). For confinement we
need the PURE key scope, so the transport sets it once per request
(``set_mcp_key_workspace_scope``) and the context-resolution chokepoints read it:

- ``_resolve_context_for_read`` (read path) forwards it to the service-layer
  chokepoint ``PermissionService.resolve_context_for_workspace_read`` as
  ``key_workspace_id`` (enforcement tested in
  ``test_permission_service.py::TestResolveContextKeyWorkspaceConfinement``).
- ``_resolve_context`` (write path: remember/update_memory/forget) enforces the
  same confinement after ``ContextService.get_context``.

These tests pin: (1) the read chokepoint forwards the contextvar value; (2) the
write chokepoint enforces it; (3) when no key scope is set (OAuth2/session/global
key → None), neither path confines — so legitimate cross-workspace reads are not
broken.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._helpers import (
    _ContextNotFoundError,
    _resolve_context,
    _resolve_context_for_read,
    set_mcp_key_workspace_scope,
)


@pytest.fixture(autouse=True)
def _reset_scope():
    """Isolate the per-request contextvar between tests (default = None)."""
    set_mcp_key_workspace_scope(None)
    yield
    set_mcp_key_workspace_scope(None)


# --- read path: _resolve_context_for_read forwards the contextvar -------------


@pytest.mark.asyncio
async def test_read_forwards_contextvar_scope_to_service():
    ws = uuid4()
    set_mcp_key_workspace_scope(ws)
    with patch("services.permission_service.PermissionService") as P:
        resolver = AsyncMock(return_value=MagicMock(workspace_id=ws))
        P.return_value.resolve_context_for_workspace_read = resolver
        await _resolve_context_for_read(AsyncMock(), "u", uuid4())
        assert resolver.await_args.kwargs["key_workspace_id"] == ws


@pytest.mark.asyncio
async def test_read_forwards_none_when_no_key_scope():
    """OAuth2/session/global key → contextvar default None → no confinement."""
    with patch("services.permission_service.PermissionService") as P:
        resolver = AsyncMock(return_value=MagicMock(workspace_id=uuid4()))
        P.return_value.resolve_context_for_workspace_read = resolver
        await _resolve_context_for_read(AsyncMock(), "u", uuid4())
        assert resolver.await_args.kwargs["key_workspace_id"] is None


@pytest.mark.asyncio
async def test_read_maps_service_denial_to_context_not_found():
    from utils.exceptions import NotFoundException

    set_mcp_key_workspace_scope(uuid4())
    with patch("services.permission_service.PermissionService") as P:
        P.return_value.resolve_context_for_workspace_read = AsyncMock(
            side_effect=NotFoundException("Context", "x")
        )
        with pytest.raises(_ContextNotFoundError):
            await _resolve_context_for_read(AsyncMock(), "u", uuid4())


# --- write path: _resolve_context enforces the contextvar ---------------------


def _patch_get_context(ctx):
    p = patch("services.context_service.ContextService")
    cls = p.start()
    cls.return_value.get_context = AsyncMock(return_value=ctx)
    return p


@pytest.mark.asyncio
async def test_write_mismatch_raises_context_not_found():
    """Workspace-scoped key (A) writing a context owned by B → uniform 404."""
    ws_a, ws_b = uuid4(), uuid4()
    set_mcp_key_workspace_scope(ws_a)
    p = _patch_get_context(MagicMock(workspace_id=ws_b))
    try:
        with pytest.raises(_ContextNotFoundError):
            await _resolve_context(AsyncMock(), "user-in-both", uuid4())
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_write_match_returns_context():
    ws = uuid4()
    ctx = MagicMock(workspace_id=ws)
    set_mcp_key_workspace_scope(ws)
    p = _patch_get_context(ctx)
    try:
        assert await _resolve_context(AsyncMock(), "u", uuid4()) is ctx
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_write_no_key_scope_skips_confinement():
    """No key scope (None) → write path not confined (membership governs)."""
    ctx = MagicMock(workspace_id=uuid4())
    p = _patch_get_context(ctx)  # contextvar is None via the autouse fixture
    try:
        assert await _resolve_context(AsyncMock(), "u", uuid4()) is ctx
    finally:
        p.stop()


# --- e2e: a handler's request scope reaches the service -----------------------


@pytest.mark.asyncio
async def test_handle_feedback_confinement_end_to_end():
    """With a key scope set (as the transport would), handle_feedback's read gate
    forwards it to the service. Guards the full handler → chokepoint → service seam."""
    from mcp_server.tools.feedback import handle_feedback

    ws_a = uuid4()
    set_mcp_key_workspace_scope(ws_a)
    mock_db = AsyncMock()

    async def mock_get_db():
        yield mock_db

    resolver = AsyncMock(return_value=MagicMock(workspace_id=ws_a))
    p_db = patch("db.base.get_db", mock_get_db)
    p_perm = patch("services.permission_service.PermissionService")
    p_fs = patch("services.feedback_service.FeedbackService")
    p_db.start()
    P = p_perm.start()
    FS = p_fs.start()
    P.return_value.resolve_context_for_workspace_read = resolver
    FS.return_value.record_feedback = AsyncMock(return_value=MagicMock(id=uuid4()))
    try:
        await handle_feedback(
            {"memory_id": str(uuid4()), "helpful": True, "context_id": str(uuid4())},
            "user",
            ws_a,
        )
        assert resolver.await_args.kwargs["key_workspace_id"] == ws_a
    finally:
        p_db.stop()
        p_perm.stop()
        p_fs.stop()
