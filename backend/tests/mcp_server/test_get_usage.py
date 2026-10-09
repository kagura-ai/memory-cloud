"""Tests for get_usage MCP tool handler."""

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
        """Create a mock workspace."""
        ws = MagicMock()
        ws.id = workspace_id
        ws.plan_name = plan_name
        return ws

    def _mock_quotas(self, memory_limit=11000, mcp_calls=1000, max_contexts=20, max_members=10):
        """Create mock effective quotas dict."""
        return {
            "memory_limit": memory_limit,
            "mcp_calls_per_day": mcp_calls,
            "max_contexts": max_contexts,
            "max_members": max_members,
        }

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

        async def mock_get_db():
            yield mock_db

        mock_effective_quota = AsyncMock()
        mock_effective_quota.get_effective_quotas.return_value = self._mock_quotas()

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.effective_quota_service.EffectiveQuotaService",
                return_value=mock_effective_quota,
            ),
            patch(
                "services.quota_service.QuotaService.count_mcp_calls_today",
                new_callable=AsyncMock,
                return_value=42,
            ),
        ):
            result = await handle_get_usage({}, user_id, workspace_id)

        import json

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["plan"] == "pro"
        assert data["memories"]["used"] == 652
        assert data["memories"]["limit"] == 11000
        assert data["contexts"]["used"] == 3
        assert data["members"]["used"] == 1
        assert data["mcp_calls_per_day"]["used"] == 42
        assert data["mcp_calls_per_day"]["limit"] == 1000

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

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_get_usage({}, user_id, workspace_id)

        import json

        data = json.loads(result[0].text)
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_zero_memory_limit(self, workspace_id, user_id):
        """Test that zero effective_memory_limit doesn't cause ZeroDivisionError."""
        ws = self._mock_workspace(workspace_id, plan_name="free")

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

        async def mock_get_db():
            yield mock_db

        mock_effective_quota = AsyncMock()
        mock_effective_quota.get_effective_quotas.return_value = self._mock_quotas(
            memory_limit=0, mcp_calls=0, max_contexts=0, max_members=0
        )

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.effective_quota_service.EffectiveQuotaService",
                return_value=mock_effective_quota,
            ),
            patch(
                "services.quota_service.QuotaService.count_mcp_calls_today",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            result = await handle_get_usage({}, user_id, workspace_id)

        import json

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["memories"]["percentage"] == 0

    @pytest.mark.asyncio
    async def test_db_error_returns_structured_error(self, workspace_id, user_id):
        """Test that DB exceptions return structured error, not unhandled exception."""
        mock_db = AsyncMock()
        mock_db.execute.side_effect = RuntimeError("connection lost")

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_get_usage({}, user_id, workspace_id)

        import json

        data = json.loads(result[0].text)
        assert data["status"] == "error"
        # #1684: the legacy code stays; the exception text stays in the log.
        assert data["error"] == "get_usage_error"
        assert data["cause"] == "internal_error"
        assert data["correlation_id"]
        assert "connection lost" not in result[0].text


class TestGetUsageCapacityLock:
    """#1941: get_usage says when the workspace is locked and how far over."""

    async def _call(self, lock):
        workspace_id = uuid4()
        ws = MagicMock()
        ws.id = workspace_id
        ws.plan_name = "free"
        results = []
        for value in (ws, 1012, 1, 1):
            r = MagicMock()
            r.scalar_one_or_none.return_value = value
            r.scalar.return_value = value
            results.append(r)
        mock_db = AsyncMock()
        mock_db.execute.side_effect = results

        async def mock_get_db():
            yield mock_db

        quotas = AsyncMock()
        quotas.get_effective_quotas.return_value = {
            "memory_limit": 1000,
            "mcp_calls_per_day": 100,
            "max_contexts": 3,
            "max_members": 1,
        }
        with (
            patch("db.base.get_db", new=mock_get_db),
            patch("services.effective_quota_service.EffectiveQuotaService", return_value=quotas),
            patch(
                "services.quota_service.QuotaService.count_mcp_calls_today",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "services.capacity_lock.capacity_lock_state",
                new_callable=AsyncMock,
                return_value=lock,
            ),
        ):
            result = await handle_get_usage({}, "u1", workspace_id)
        import json

        return json.loads(result[0].text)

    @pytest.mark.asyncio
    async def test_null_when_not_locked(self):
        data = await self._call(None)
        assert data["status"] == "success"
        assert data["capacity_lock"] is None

    @pytest.mark.asyncio
    async def test_the_numbers_and_the_way_out_when_locked(self):
        from services.capacity_lock import CapacityLock

        lock = CapacityLock(
            memory_count=1012,
            memory_limit=1000,
            over_memories=12,
            used_bytes=0,
            storage_limit_bytes=100,
            over_bytes=0,
            cleanup_url="https://app.example.test/workspace/settings/plan",
        )
        data = await self._call(lock)
        block = data["capacity_lock"]
        assert block["over_memories"] == 12
        assert block["cleanup_url"].endswith("/workspace/settings/plan")
        assert "12 memories" in block["help"]
