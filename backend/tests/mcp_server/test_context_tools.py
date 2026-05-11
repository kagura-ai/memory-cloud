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

from mcp_server.tools.context import handle_delete_context, handle_update_context
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
