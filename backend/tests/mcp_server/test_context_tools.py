"""Tests for MCP context tool handlers (Issue #401).

Locks down the AuthorizationError / NotFoundException response surface for
``handle_delete_context``. The branches at ``mcp_server/tools/context.py``
lines 691 (NotFoundException → ``context_not_found``) and 697-700
(AuthorizationError → ``permission_denied``) were dead code before #401:
``PermissionService.check_context_owner`` raised ``HTTPException``, which
matched neither domain-exception branch and fell into the generic 500
``delete_context_error`` path.

After #401 the service raises domain exceptions and these branches are live
— this test pins that contract so a future refactor of the MCP error
surface can't silently revert it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._helpers import _ContextNotFoundError
from mcp_server.tools.context import (
    handle_delete_context,
    handle_merge_contexts,
    handle_update_context,
)
from utils.exceptions import AuthorizationError, NotFoundException


class TestHandleDeleteContextErrorSurface:
    @pytest.fixture
    def user_id(self):
        return "test_user_401"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_authorization_error_returns_permission_denied(
        self, user_id, workspace_id, context_id
    ):
        """check_context_owner AuthorizationError → MCP ``permission_denied``.

        Issue #401: this branch became live when PermissionService swapped
        to domain exceptions. Before the refactor it was dead — the
        HTTPException(403) raised by check_context_owner fell into the
        generic Exception 500 path instead.
        """
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_perm = MagicMock()
        mock_perm.check_context_owner = AsyncMock(
            side_effect=AuthorizationError("Insufficient permissions")
        )

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.permission_service.PermissionService",
                return_value=mock_perm,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_delete_context(
                args={"context_id": str(context_id)},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "permission_denied"
        mock_db.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_not_found_exception_returns_context_not_found(
        self, user_id, workspace_id, context_id
    ):
        """check_context_owner NotFoundException → MCP ``context_not_found``.

        Same uniform-disclosure contract as test_graph_visibility's 404
        cases: regardless of whether the context truly doesn't exist or
        the caller can't see it, the MCP surface emits ``context_not_found``.
        """
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_perm = MagicMock()
        mock_perm.check_context_owner = AsyncMock(
            side_effect=NotFoundException("Context", str(context_id))
        )

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.permission_service.PermissionService",
                return_value=mock_perm,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_delete_context(
                args={"context_id": str(context_id)},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "context_not_found"
        mock_db.rollback.assert_awaited()


class TestHandleMergeContextsWorkspaceBoundary:
    """Issue #966: ``handle_merge_contexts`` must apply the workspace-boundary
    guard at the MCP boundary (mirror of ``handle_recall``), resolving both
    source and target via ``_resolve_context_for_read`` and rejecting a
    cross-workspace merge with a uniform ``workspace_mismatch`` error before
    ``ContextService.merge_contexts`` is ever called.

    Without the guard a member of two workspaces could probe / merge across
    the workspace boundary, and an access-denied context leaks through the
    generic ``merge_contexts_error`` 500 envelope (CWE-639) instead of the
    uniform ``context_not_found`` shape.
    """

    @pytest.fixture
    def user_id(self):
        return "test_user_966"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def source_id(self):
        return uuid4()

    @pytest.fixture
    def target_id(self):
        return uuid4()

    def _ctx(self, workspace_id):
        ctx = MagicMock()
        ctx.workspace_id = workspace_id
        return ctx

    @pytest.mark.asyncio
    async def test_cross_workspace_merge_returns_workspace_mismatch(
        self, user_id, workspace_id, source_id, target_id
    ):
        """Source and target in different workspaces → ``workspace_mismatch``,
        and ``merge_contexts`` is never invoked."""
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        source_ctx = self._ctx(uuid4())
        target_ctx = self._ctx(uuid4())  # different workspace

        resolve = AsyncMock(side_effect=[source_ctx, target_ctx])
        mock_service = MagicMock()
        mock_service.merge_contexts = AsyncMock()

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.context._resolve_context_for_read",
                new=resolve,
            ),
            patch(
                "services.context_service.ContextService",
                return_value=mock_service,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_merge_contexts(
                args={"source_id": str(source_id), "target_id": str(target_id)},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "workspace_mismatch"
        mock_service.merge_contexts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreadable_context_returns_context_not_found(
        self, user_id, workspace_id, source_id, target_id
    ):
        """A context the caller can't read → uniform ``context_not_found``
        (not the generic ``merge_contexts_error`` 500 leak), and
        ``merge_contexts`` is never invoked."""
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        resolve = AsyncMock(
            side_effect=_ContextNotFoundError(
                source_id, "Context not found or you don't have access to it."
            )
        )
        mock_service = MagicMock()
        mock_service.merge_contexts = AsyncMock()

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.context._resolve_context_for_read",
                new=resolve,
            ),
            patch(
                "services.context_service.ContextService",
                return_value=mock_service,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_merge_contexts(
                args={"source_id": str(source_id), "target_id": str(target_id)},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "context_not_found"
        mock_service.merge_contexts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_workspace_proceeds_to_merge(
        self, user_id, workspace_id, source_id, target_id
    ):
        """Both contexts in the same workspace → guard passes and
        ``merge_contexts`` runs."""
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()
        mock_db.commit = AsyncMock()

        async def mock_get_db():
            yield mock_db

        shared_ws = uuid4()
        resolve = AsyncMock(side_effect=[self._ctx(shared_ws), self._ctx(shared_ws)])
        mock_service = MagicMock()
        mock_service.merge_contexts = AsyncMock(return_value={"merged": 3, "source_deleted": False})

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.context._resolve_context_for_read",
                new=resolve,
            ),
            patch(
                "services.context_service.ContextService",
                return_value=mock_service,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_merge_contexts(
                args={"source_id": str(source_id), "target_id": str(target_id)},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "success"
        assert payload["merged"] == 3
        mock_service.merge_contexts.assert_awaited_once()


class TestHandleUpdateContextErrorSurface:
    """Pins the domain-exception envelope contract for handle_update_context
    (mirror of handle_delete_context). AuthorizationError → permission_denied
    with exc.message (CWE-639 uniform string). NotFoundException →
    context_not_found.
    """

    @pytest.fixture
    def user_id(self):
        return "test_user_604"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_authorization_error_returns_permission_denied(
        self, user_id, workspace_id, context_id
    ):
        """check_context_owner AuthorizationError → MCP ``permission_denied``.

        The response message must come from ``exc.message`` (the
        AuthorizationError-enforced uniform ``"Insufficient permissions"``),
        not ``str(exc)`` which could vary across str-coercion edge cases.
        """
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_perm = MagicMock()
        mock_perm.check_context_owner = AsyncMock(
            side_effect=AuthorizationError("Insufficient permissions")
        )

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.permission_service.PermissionService",
                return_value=mock_perm,
            ),
        ):
            result = await handle_update_context(
                args={"context_id": str(context_id), "summary": "new summary"},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "permission_denied"
        assert payload["message"] == "Insufficient permissions"
        mock_db.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_not_found_exception_returns_context_not_found(
        self, user_id, workspace_id, context_id
    ):
        """check_context_owner NotFoundException → MCP ``context_not_found``.

        Same uniform-disclosure contract as the delete-path test: regardless
        of whether the context truly doesn't exist or the caller can't see
        it, the MCP surface emits ``context_not_found``.
        """
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_perm = MagicMock()
        mock_perm.check_context_owner = AsyncMock(
            side_effect=NotFoundException("Context", str(context_id))
        )

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.permission_service.PermissionService",
                return_value=mock_perm,
            ),
        ):
            result = await handle_update_context(
                args={"context_id": str(context_id), "summary": "new summary"},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "context_not_found"
        mock_db.rollback.assert_awaited()
