"""Tests for MCP rate limit enforcement (Issue #149).

Tests:
- QuotaService.check_mcp_rate_limit: DB-level quota check
- execute_tool_call rate limit integration: pre-dispatch gating
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.quota_service import QuotaService


class TestCheckMcpRateLimit:
    """Test QuotaService.check_mcp_rate_limit()."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return QuotaService(mock_db)

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    def _mock_workspace(self, workspace_id, plan_name="pro", mcp_calls_per_day=1000):
        ws = MagicMock()
        ws.id = workspace_id
        ws.plan_name = plan_name
        ws.effective_mcp_calls_per_day = mcp_calls_per_day
        return ws

    @pytest.mark.asyncio
    async def test_within_limit(self, service, mock_db, workspace_id):
        """Allow when used < limit."""
        ws = self._mock_workspace(workspace_id, mcp_calls_per_day=1000)
        count_result = MagicMock()
        count_result.scalar.return_value = 500
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.side_effect = [ws_result, count_result]

        allowed, used, limit = await service.check_mcp_rate_limit(workspace_id)

        assert allowed is True
        assert used == 500
        assert limit == 1000

    @pytest.mark.asyncio
    async def test_at_limit(self, service, mock_db, workspace_id):
        """Deny when used == limit."""
        ws = self._mock_workspace(workspace_id, mcp_calls_per_day=100)
        count_result = MagicMock()
        count_result.scalar.return_value = 100
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.side_effect = [ws_result, count_result]

        allowed, used, limit = await service.check_mcp_rate_limit(workspace_id)

        assert allowed is False
        assert used == 100
        assert limit == 100

    @pytest.mark.asyncio
    async def test_over_limit(self, service, mock_db, workspace_id):
        """Deny when used > limit."""
        ws = self._mock_workspace(workspace_id, mcp_calls_per_day=50)
        count_result = MagicMock()
        count_result.scalar.return_value = 55
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.side_effect = [ws_result, count_result]

        allowed, used, limit = await service.check_mcp_rate_limit(workspace_id)

        assert allowed is False
        assert used == 55
        assert limit == 50

    @pytest.mark.asyncio
    async def test_workspace_not_found(self, service, mock_db, workspace_id):
        """Raise ValueError when workspace not found."""
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, count_result]

        with pytest.raises(ValueError, match="not found"):
            await service.check_mcp_rate_limit(workspace_id)

    @pytest.mark.asyncio
    async def test_zero_usage(self, service, mock_db, workspace_id):
        """Allow when no calls today."""
        ws = self._mock_workspace(workspace_id, mcp_calls_per_day=100)
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.side_effect = [ws_result, count_result]

        allowed, used, limit = await service.check_mcp_rate_limit(workspace_id)

        assert allowed is True
        assert used == 0


class TestExecuteToolCallRateLimit:
    """Test rate limit integration in execute_tool_call."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Clear rate limit cache before each test."""
        from mcp_server.tools import invalidate_rate_limit_cache

        invalidate_rate_limit_cache()
        yield
        invalidate_rate_limit_cache()

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def user_id(self):
        return "test_user"

    @pytest.mark.asyncio
    async def test_rate_limited_tool_blocked(self, workspace_id, user_id):
        """Non-exempt tool is blocked when rate limit exceeded."""
        import json

        from mcp_server.tools import execute_tool_call

        with patch(
            "mcp_server.tools._check_rate_limit",
            new_callable=AsyncMock,
            return_value=(False, 100, 100),
        ):
            result = await execute_tool_call(
                tool_name="remember",
                arguments={"context_id": str(uuid4()), "summary": "test"},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        response = json.loads(result[0].text)
        assert response["status"] == "error"
        assert response["error"] == "rate_limit_exceeded"
        assert response["used_today"] == 100
        assert response["daily_limit"] == 100

    @pytest.mark.asyncio
    async def test_exempt_tool_not_blocked(self, workspace_id, user_id):
        """Exempt tools bypass rate limit check."""
        import json

        from mcp_server.tools import execute_tool_call

        # Mock _check_rate_limit to return denied — but get_usage is exempt
        with (
            patch(
                "mcp_server.tools._check_rate_limit",
                new_callable=AsyncMock,
                return_value=(False, 100, 100),
            ) as mock_check,
            patch(
                "mcp_server.tools.usage.handle_get_usage", new_callable=AsyncMock
            ) as mock_handler,
        ):
            from mcp.types import TextContent

            mock_handler.return_value = [
                TextContent(type="text", text=json.dumps({"status": "success"}))
            ]

            await execute_tool_call(
                tool_name="get_usage",
                arguments={},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        # Rate limit check should NOT have been called
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_check_failure_allows_tool(self, workspace_id, user_id):
        """Tool execution continues when rate limit check raises an exception."""
        import json

        from mcp.types import TextContent

        import mcp_server.tools as tools_module
        from mcp_server.tools import execute_tool_call

        mock_handler = AsyncMock(
            return_value=[TextContent(type="text", text=json.dumps({"status": "success"}))]
        )

        # Ensure registry is built, then patch the entry directly
        if tools_module._TOOL_REGISTRY is None:
            tools_module._TOOL_REGISTRY = tools_module._build_registry()
        original = tools_module._TOOL_REGISTRY["recall"]

        try:
            tools_module._TOOL_REGISTRY["recall"] = mock_handler
            with patch(
                "mcp_server.tools._check_rate_limit",
                new_callable=AsyncMock,
                side_effect=Exception("DB connection error"),
            ):
                result = await execute_tool_call(
                    tool_name="recall",
                    arguments={"context_id": str(uuid4()), "query": "test"},
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
        finally:
            tools_module._TOOL_REGISTRY["recall"] = original

        # Tool should still execute despite rate limit check failure
        response = json.loads(result[0].text)
        assert response["status"] == "success"
        mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_workspace_skips_rate_limit(self, user_id):
        """Rate limit check is skipped when workspace_id is None."""
        from mcp_server.tools import execute_tool_call

        with patch(
            "mcp_server.tools._check_rate_limit",
            new_callable=AsyncMock,
        ) as mock_check:
            # Will fail at context_id validation, but rate limit should be skipped
            await execute_tool_call(
                tool_name="remember",
                arguments={},
                user_id=user_id,
                workspace_id=None,
            )

        mock_check.assert_not_called()
