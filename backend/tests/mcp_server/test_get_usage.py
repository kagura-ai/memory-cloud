"""Tests for get_usage MCP tool handler (Issue #82)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.usage import handle_get_usage


class TestGetUsage:
    """Test get_usage MCP tool handler."""

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def user_id(self):
        return "test_user_123"

    def _mock_workspace(self, workspace_id, plan_name="pro"):
        """Create a mock workspace with effective properties."""
        from config.plan_tiers import get_plan_tier

        tier = get_plan_tier(plan_name)
        ws = MagicMock()
        ws.id = workspace_id
        ws.plan_name = plan_name
        ws.effective_memory_limit = 11000
        ws.effective_mcp_calls_per_day = tier.mcp_calls_per_day
        ws.effective_max_contexts = tier.max_contexts_per_workspace
        ws.effective_max_members = tier.max_members_per_workspace
        return ws

    @pytest.mark.asyncio
    async def test_returns_usage(self, workspace_id, user_id):
        """Test that get_usage returns correct quota/usage data."""
        ws = self._mock_workspace(workspace_id)

        mock_db = AsyncMock()
        # 4 queries: workspace, memory count, context count, member count
        mock_ws_result = MagicMock()
        mock_ws_result.scalar_one_or_none.return_value = ws
        mock_mem_result = MagicMock()
        mock_mem_result.scalar.return_value = 652
        mock_ctx_result = MagicMock()
        mock_ctx_result.scalar.return_value = 3
        mock_member_result = MagicMock()
        mock_member_result.scalar.return_value = 1

        mock_db.execute.side_effect = [
            mock_ws_result,
            mock_mem_result,
            mock_ctx_result,
            mock_member_result,
        ]

        mock_db_ctx = AsyncMock()
        mock_db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("db.base.get_db", return_value=mock_db_ctx):
            result = await handle_get_usage({}, user_id, workspace_id)

        import json

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["plan"] == "pro"
        assert data["memories"]["used"] == 652
        assert data["memories"]["limit"] == 11000
        assert data["contexts"]["used"] == 3
        assert data["members"]["used"] == 1

    @pytest.mark.asyncio
    async def test_no_workspace_returns_error(self, user_id):
        """Test that missing workspace_id returns error."""
        result = await handle_get_usage({}, user_id, None)

        import json

        data = json.loads(result[0].text)
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_workspace_not_found(self, workspace_id, user_id):
        """Test that non-existent workspace returns error."""
        mock_db = AsyncMock()
        mock_ws_result = MagicMock()
        mock_ws_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_ws_result

        mock_db_ctx = AsyncMock()
        mock_db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("db.base.get_db", return_value=mock_db_ctx):
            result = await handle_get_usage({}, user_id, workspace_id)

        import json

        data = json.loads(result[0].text)
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_zero_memory_limit(self, workspace_id, user_id):
        """Test that zero effective_memory_limit doesn't cause ZeroDivisionError."""
        ws = MagicMock()
        ws.id = workspace_id
        ws.plan_name = "free"
        ws.effective_memory_limit = 0
        ws.effective_mcp_calls_per_day = 0
        ws.effective_max_contexts = 0
        ws.effective_max_members = 0

        mock_db = AsyncMock()
        mock_ws_result = MagicMock()
        mock_ws_result.scalar_one_or_none.return_value = ws
        mock_mem_result = MagicMock()
        mock_mem_result.scalar.return_value = 0
        mock_ctx_result = MagicMock()
        mock_ctx_result.scalar.return_value = 0
        mock_member_result = MagicMock()
        mock_member_result.scalar.return_value = 0

        mock_db.execute.side_effect = [
            mock_ws_result,
            mock_mem_result,
            mock_ctx_result,
            mock_member_result,
        ]

        mock_db_ctx = AsyncMock()
        mock_db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("db.base.get_db", return_value=mock_db_ctx):
            result = await handle_get_usage({}, user_id, workspace_id)

        import json

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["memories"]["percentage"] == 0
