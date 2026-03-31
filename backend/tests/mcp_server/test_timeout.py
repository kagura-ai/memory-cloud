"""Tests for MCP tool timeout mechanism."""

import asyncio

import pytest

from mcp_server.tools._helpers import execute_with_timeout


class TestExecuteWithTimeout:
    """Test execute_with_timeout."""

    @pytest.mark.asyncio
    async def test_fast_operation_succeeds(self):
        """Fast coroutine completes within timeout."""

        async def fast():
            return "done"

        result = await execute_with_timeout(fast(), timeout=5.0, operation_name="test")
        assert result == "done"

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        """Slow coroutine raises TimeoutError."""

        async def slow():
            await asyncio.sleep(10)

        with pytest.raises(TimeoutError):
            await execute_with_timeout(slow(), timeout=0.1, operation_name="test")

    @pytest.mark.asyncio
    async def test_exception_propagates(self):
        """Exceptions from coroutine propagate correctly."""

        async def failing():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            await execute_with_timeout(failing(), timeout=5.0, operation_name="test")

    @pytest.mark.asyncio
    async def test_return_value_preserved(self):
        """Return value of coroutine is preserved."""

        async def compute():
            return {"key": "value", "count": 42}

        result = await execute_with_timeout(compute(), timeout=5.0, operation_name="test")
        assert result == {"key": "value", "count": 42}
