"""MCP → service forwarding of the key workspace scope (Issue #963).

Confinement *enforcement* lives in
``PermissionService.resolve_context_for_workspace_read`` (the single chokepoint
shared by MCP and REST reads) and is tested in
``test_permission_service.py::TestResolveContextKeyWorkspaceConfinement``.

These tests pin the WIRING on the MCP side: the chokepoint
``_resolve_context_for_read`` forwards its ``workspace_id`` as
``key_workspace_id``, and an MCP read handler threads its key workspace all the
way down to the service. A dropped forward at any layer silently reopens #963,
so these guard the seam.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._helpers import _ContextNotFoundError, _resolve_context_for_read


@pytest.mark.asyncio
async def test_resolve_forwards_key_workspace_id_to_service():
    """_resolve_context_for_read must pass its workspace_id through as the
    service's key_workspace_id (else confinement never runs)."""
    ws = uuid4()
    ctx = MagicMock(workspace_id=ws)
    with patch("services.permission_service.PermissionService") as P:
        resolver = AsyncMock(return_value=ctx)
        P.return_value.resolve_context_for_workspace_read = resolver
        await _resolve_context_for_read(AsyncMock(), "u", uuid4(), workspace_id=ws)
        assert resolver.await_args.kwargs["key_workspace_id"] == ws


@pytest.mark.asyncio
async def test_resolve_maps_service_denial_to_context_not_found():
    """A service-layer deny (NotFoundException, incl. the #963 key mismatch)
    surfaces as the MCP-native uniform _ContextNotFoundError."""
    from utils.exceptions import NotFoundException

    with patch("services.permission_service.PermissionService") as P:
        P.return_value.resolve_context_for_workspace_read = AsyncMock(
            side_effect=NotFoundException("Context", "x")
        )
        with pytest.raises(_ContextNotFoundError):
            await _resolve_context_for_read(AsyncMock(), "u", uuid4(), workspace_id=uuid4())


@pytest.mark.asyncio
async def test_handle_feedback_threads_key_workspace_end_to_end():
    """End-to-end wiring guard: a handler's key workspace_id reaches the service
    as key_workspace_id. If a future refactor drops workspace_id= at the call
    site, this goes red — the exact #963 regression."""
    from mcp_server.tools.feedback import handle_feedback

    ws_a = uuid4()
    ctx = MagicMock(workspace_id=ws_a)
    mock_db = AsyncMock()

    async def mock_get_db():
        yield mock_db

    resolver = AsyncMock(return_value=ctx)
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


@pytest.mark.asyncio
async def test_handle_merge_contexts_confines_both_resolves():
    """N-context wiring guard: handle_merge_contexts resolves BOTH source and
    target; both must forward the key workspace. A copy-paste-miss that forwards
    on the first resolve but not the second would leave a confinement hole on
    the merge target — this asserts both calls carry workspace_id."""
    from mcp_server.tools.context import handle_merge_contexts

    ws = uuid4()
    same_ws_ctx = MagicMock(workspace_id=ws)  # source/target same ws → no mismatch
    mock_db = AsyncMock()

    async def mock_get_db():
        yield mock_db

    resolve = AsyncMock(return_value=same_ws_ctx)
    p_db = patch("db.base.get_db", mock_get_db)
    p_resolve = patch("mcp_server.tools.context._resolve_context_for_read", resolve)
    p_svc = patch("services.context_service.ContextService")
    p_log = patch("mcp_server.tools.context._log_tool_usage", AsyncMock())
    p_db.start()
    p_resolve.start()
    SVC = p_svc.start()
    p_log.start()
    SVC.return_value.merge_contexts = AsyncMock(return_value={"merged": 0})
    try:
        await handle_merge_contexts(
            {"source_id": str(uuid4()), "target_id": str(uuid4())},
            "user",
            ws,
        )
        assert resolve.await_count == 2, "both source and target must be resolved"
        assert all(call.kwargs.get("workspace_id") == ws for call in resolve.await_args_list), (
            "both resolves must forward the key workspace_id"
        )
    finally:
        p_db.stop()
        p_resolve.stop()
        p_svc.stop()
        p_log.stop()
