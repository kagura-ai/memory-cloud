"""Regression tests for GET /memory/stats cross-workspace disclosure (Issue #1011).

``memory.py`` previously raised a raw ``403`` ("This context belongs to a
different workspace. Please switch workspaces first.") whenever the requested
``context_id`` lived in a workspace other than the caller's *current* one. That
403 distinguished "exists elsewhere" from "not found" — a cross-workspace
existence oracle (CWE-639 / OWASP A01).

The fix routes resolution through
``PermissionService.resolve_context_for_workspace_read``, which returns a
uniform ``404`` (``NotFoundException``) on not-found / non-member /
private-non-creator — identical to the sibling ``GET /memory/list`` route
(``test_memory_list.py``) and the graph routes. A member of the context's
*owning* workspace can read its stats regardless of which workspace is
currently active (no "switch workspaces" dance), while a non-member can no
longer tell whether the context exists at all.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes.memory import get_access_patterns, get_memory_stats
from utils.exceptions import NotFoundException

MOCK_USER = {"user_id": "test_user_123"}


def _stats_result(total: int = 0):
    r = MagicMock()
    r.total_count = total
    return r


def _db_with_workspace(owner_user_id: str | None):
    """AsyncMock db whose single .execute() returns a Workspace owner-check row."""
    db = AsyncMock()
    ws = MagicMock()
    ws.owner_user_id = owner_user_id
    result = MagicMock()
    result.scalar_one_or_none.return_value = ws
    db.execute.return_value = result
    return db


def _ctx(workspace_id, *, is_private: bool):
    ctx = MagicMock()
    ctx.workspace_id = workspace_id
    ctx.is_private = is_private
    return ctx


@pytest.mark.asyncio
async def test_stats_cross_workspace_probe_returns_uniform_404():
    """A context the caller cannot read surfaces as NotFoundException (uniform
    404), NOT a 403 that confirms it exists in another workspace (CWE-639)."""
    context_id = uuid4()
    mock_db = AsyncMock()  # must not be reached for the Workspace query
    memory_service = MagicMock()
    memory_service.get_stats = AsyncMock(return_value=_stats_result())

    perm = MagicMock()
    perm.resolve_context_for_workspace_read = AsyncMock(
        side_effect=NotFoundException("Context", str(context_id))
    )

    with patch("api.routes.memory.PermissionService", return_value=perm):
        with pytest.raises(NotFoundException) as exc:
            await get_memory_stats(
                user=MOCK_USER,
                context_id=context_id,
                memory_service=memory_service,
                db=mock_db,
            )

    assert exc.value.status_code == 404
    # A forbidden probe must not silently fall through to "private stats".
    memory_service.get_stats.assert_not_awaited()


@pytest.mark.asyncio
async def test_stats_no_context_id_skips_permission_check():
    """Without context_id the permission chokepoint is not consulted and stats
    run unscoped (legacy behavior, no regression)."""
    mock_db = AsyncMock()
    memory_service = MagicMock()
    memory_service.get_stats = AsyncMock(return_value=_stats_result(3))

    with patch("api.routes.memory.PermissionService") as perm_cls:
        result = await get_memory_stats(
            user=MOCK_USER,
            context_id=None,
            memory_service=memory_service,
            db=mock_db,
        )

    perm_cls.assert_not_called()
    assert result.total_count == 3
    memory_service.get_stats.assert_awaited_once_with(
        user_id="test_user_123",
        workspace_id=None,
        context_id=None,
        include_details=True,
        time_window_hours=24,
        is_shared_context=False,
    )


@pytest.mark.asyncio
async def test_stats_shared_context_aggregates_across_members():
    """Non-private (shared) context → is_shared_context=True so every member's
    memories are counted, scoped to the context's OWNING workspace."""
    context_id = uuid4()
    ws_id = uuid4()
    mock_db = _db_with_workspace(owner_user_id="someone_else")  # caller is not owner
    memory_service = MagicMock()
    memory_service.get_stats = AsyncMock(return_value=_stats_result(7))

    perm = MagicMock()
    perm.resolve_context_for_workspace_read = AsyncMock(return_value=_ctx(ws_id, is_private=False))

    with patch("api.routes.memory.PermissionService", return_value=perm) as perm_cls:
        await get_memory_stats(
            user=MOCK_USER,
            context_id=context_id,
            memory_service=memory_service,
            db=mock_db,
        )

    perm_cls.assert_called_once_with(mock_db)
    perm.resolve_context_for_workspace_read.assert_awaited_once_with(
        user_id="test_user_123", context_id=context_id, key_workspace_id=None
    )
    memory_service.get_stats.assert_awaited_once_with(
        user_id="test_user_123",
        workspace_id=str(ws_id),
        context_id=str(context_id),
        include_details=True,
        time_window_hours=24,
        is_shared_context=True,
    )


@pytest.mark.asyncio
async def test_stats_owner_sees_private_context_as_shared():
    """Workspace owner sees all memories even in a private context
    (is_shared_context=True), anchored to the owning workspace."""
    context_id = uuid4()
    ws_id = uuid4()
    mock_db = _db_with_workspace(owner_user_id="test_user_123")  # caller IS owner
    memory_service = MagicMock()
    memory_service.get_stats = AsyncMock(return_value=_stats_result(2))

    perm = MagicMock()
    perm.resolve_context_for_workspace_read = AsyncMock(return_value=_ctx(ws_id, is_private=True))

    with patch("api.routes.memory.PermissionService", return_value=perm):
        await get_memory_stats(
            user=MOCK_USER,
            context_id=context_id,
            memory_service=memory_service,
            db=mock_db,
        )

    _, kwargs = memory_service.get_stats.await_args
    assert kwargs["is_shared_context"] is True


@pytest.mark.asyncio
async def test_stats_non_owner_private_context_is_creator_scoped():
    """Non-owner viewing a private context → is_shared_context=False (creator
    scoping preserved)."""
    context_id = uuid4()
    ws_id = uuid4()
    mock_db = _db_with_workspace(owner_user_id="someone_else")
    memory_service = MagicMock()
    memory_service.get_stats = AsyncMock(return_value=_stats_result(1))

    perm = MagicMock()
    perm.resolve_context_for_workspace_read = AsyncMock(return_value=_ctx(ws_id, is_private=True))

    with patch("api.routes.memory.PermissionService", return_value=perm):
        await get_memory_stats(
            user=MOCK_USER,
            context_id=context_id,
            memory_service=memory_service,
            db=mock_db,
        )

    _, kwargs = memory_service.get_stats.await_args
    assert kwargs["is_shared_context"] is False


@pytest.mark.asyncio
async def test_stats_forwards_api_key_workspace_scope():
    """Issue #963 parity: a workspace-scoped API key forwards its scope as
    key_workspace_id so /memory/stats is confined like /memory/list."""
    context_id = uuid4()
    ws_id = uuid4()
    key_ws = uuid4()
    mock_db = _db_with_workspace(owner_user_id="someone_else")
    memory_service = MagicMock()
    memory_service.get_stats = AsyncMock(return_value=_stats_result())

    perm = MagicMock()
    perm.resolve_context_for_workspace_read = AsyncMock(return_value=_ctx(ws_id, is_private=False))

    with patch("api.routes.memory.PermissionService", return_value=perm):
        await get_memory_stats(
            user={"user_id": "test_user_123", "api_key_workspace_id": key_ws},
            context_id=context_id,
            memory_service=memory_service,
            db=mock_db,
        )

    perm.resolve_context_for_workspace_read.assert_awaited_once_with(
        user_id="test_user_123", context_id=context_id, key_workspace_id=key_ws
    )


# --- GET /access-patterns: the sibling route converged in the same fix -------


def _db_for_access_patterns():
    """AsyncMock db answering the 3 analytics queries with empty results."""
    db = AsyncMock()
    most = MagicMock()
    most.scalars.return_value.all.return_value = []
    typed = MagicMock()
    typed.all.return_value = []
    recent = MagicMock()
    recent.scalar.return_value = 0
    db.execute.side_effect = [most, typed, recent]
    return db


@pytest.mark.asyncio
async def test_access_patterns_cross_workspace_probe_returns_uniform_404():
    """The /access-patterns sibling must also surface a forbidden/cross-workspace
    probe as a uniform 404 — and NOT have the helper's NotFoundException masked
    as a 500 by the route's broad ``except Exception`` handler."""
    context_id = uuid4()
    mock_db = AsyncMock()

    perm = MagicMock()
    perm.resolve_context_for_workspace_read = AsyncMock(
        side_effect=NotFoundException("Context", str(context_id))
    )

    with patch("api.routes.memory.PermissionService", return_value=perm):
        with pytest.raises(NotFoundException) as exc:
            await get_access_patterns(
                user=MOCK_USER,
                context_id=context_id,
                db=mock_db,
                days=30,
            )

    # 404 (uniform not-found), not 500 (which the broad except would have produced).
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_access_patterns_forwards_api_key_workspace_scope():
    """Issue #963 parity: /access-patterns confines a workspace-scoped API key
    via key_workspace_id, like /stats and /list."""
    context_id = uuid4()
    ws_id = uuid4()
    key_ws = uuid4()
    mock_db = _db_for_access_patterns()

    perm = MagicMock()
    perm.resolve_context_for_workspace_read = AsyncMock(return_value=_ctx(ws_id, is_private=False))

    with patch("api.routes.memory.PermissionService", return_value=perm):
        await get_access_patterns(
            user={"user_id": "test_user_123", "api_key_workspace_id": key_ws},
            context_id=context_id,
            db=mock_db,
            days=30,
        )

    perm.resolve_context_for_workspace_read.assert_awaited_once_with(
        user_id="test_user_123", context_id=context_id, key_workspace_id=key_ws
    )
