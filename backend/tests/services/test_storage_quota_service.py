"""Tests for ``services.storage_quota_service`` (Issue #485).

Mirrors the patterns in ``test_resource_ingest_quota.py`` (the canonical
#332 shared-quota test). Patches ``db.redis.*`` calls because the
service is the only thing we want to exercise — the Redis layer itself
is tested elsewhere.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services import storage_quota_service
from utils.exceptions import QuotaExceededError, RedisError


@pytest.fixture
def workspace_id():
    return uuid4()


class TestReserveStorageBytes:
    @pytest.mark.asyncio
    async def test_within_quota_increments_counter(self, workspace_id):
        """Reservation within ceiling → atomic Lua INCRBY, no exception."""
        db = MagicMock()
        with (
            patch.object(storage_quota_service, "get_cache", AsyncMock(return_value="0")),
            patch.object(
                storage_quota_service,
                "_atomic_check_and_incr",
                AsyncMock(return_value=(1024, 0)),  # (new_total, current_at_lua)
            ) as atomic,
        ):
            await storage_quota_service.reserve_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=1024,
                quota_bytes=10_000,
                db=db,
            )
            atomic.assert_awaited_once()
            args, _ = atomic.call_args
            # _atomic_check_and_incr(key, size_bytes, quota_bytes)
            assert args[1] == 1024
            assert args[2] == 10_000

    @pytest.mark.asyncio
    async def test_at_ceiling_raises_quota_exceeded(self, workspace_id):
        """Lua returns (-1, current_at_lua) → QuotaExceededError uses Lua-time current."""
        db = MagicMock()
        with (
            patch.object(storage_quota_service, "get_cache", AsyncMock(return_value="9500")),
            patch.object(
                storage_quota_service,
                "_atomic_check_and_incr",
                AsyncMock(return_value=(-1, 9500)),  # (sentinel, current_at_lua)
            ) as atomic,
        ):
            with pytest.raises(QuotaExceededError, match="10500 / 10000") as exc_info:
                await storage_quota_service.reserve_storage_bytes(
                    workspace_id=workspace_id,
                    size_bytes=1000,
                    quota_bytes=10_000,
                    db=db,
                )
            # The atomic helper was awaited (the cap check is inside the
            # Lua script now, not in Python).
            atomic.assert_awaited_once()
            # The error payload uses the Lua-observed current value, not
            # the seed-time value (#554 Copilot loop 1 fix).
            assert exc_info.value.details.get("current") == 9500

    @pytest.mark.asyncio
    async def test_at_ceiling_uses_lua_current_not_seed(self, workspace_id):
        """When concurrent reservations land between seed and Lua, the
        error message must report the Lua-observed value, not the
        stale seed-time snapshot. Without the tuple-return fix, the
        operator-facing log/exception would underreport usage."""
        db = MagicMock()
        with (
            # Seed read 8000 (stale by the time Lua runs).
            patch.object(storage_quota_service, "get_cache", AsyncMock(return_value="8000")),
            patch.object(
                storage_quota_service,
                "_atomic_check_and_incr",
                # Lua actually saw 9800 (concurrent reservation landed).
                AsyncMock(return_value=(-1, 9800)),
            ),
        ):
            with pytest.raises(QuotaExceededError) as exc_info:
                await storage_quota_service.reserve_storage_bytes(
                    workspace_id=workspace_id,
                    size_bytes=500,
                    quota_bytes=10_000,
                    db=db,
                )
            # Must report 9800 (Lua-time), not 8000 (seed-time).
            assert exc_info.value.details.get("current") == 9800
            assert "10300 / 10000" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_quota_zero_disables_check(self, workspace_id):
        """quota_bytes <= 0 → skip the Redis round-trip entirely."""
        db = MagicMock()
        with (
            patch.object(storage_quota_service, "get_cache", AsyncMock()) as get_cache,
            patch.object(
                storage_quota_service,
                "_atomic_check_and_incr",
                AsyncMock(),
            ) as atomic,
        ):
            await storage_quota_service.reserve_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=1024,
                quota_bytes=0,
                db=db,
            )
            get_cache.assert_not_awaited()
            atomic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_size_raises_value_error(self, workspace_id):
        """Defensive: caller MUST pass a positive size."""
        db = MagicMock()
        with pytest.raises(ValueError, match="size_bytes must be > 0"):
            await storage_quota_service.reserve_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=-1,
                quota_bytes=10_000,
                db=db,
            )

    @pytest.mark.asyncio
    async def test_redis_error_on_seed_swallowed_fail_open(self, workspace_id):
        """RedisError raised during the seed step MUST NOT block uploads."""
        db = MagicMock()
        with (
            patch.object(
                storage_quota_service,
                "get_cache",
                AsyncMock(side_effect=RedisError("redis down")),
            ),
            patch.object(
                storage_quota_service,
                "_atomic_check_and_incr",
                AsyncMock(),
            ) as atomic,
        ):
            # No exception should bubble up; reservation simply skipped.
            await storage_quota_service.reserve_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=1024,
                quota_bytes=10_000,
                db=db,
            )
            atomic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redis_error_on_atomic_script_swallowed_fail_open(self, workspace_id):
        """RedisError raised by the Lua EVAL MUST NOT block uploads.

        Mirrors the fail-open posture documented at the module level —
        the cap check is advisory when Redis is unhealthy; the next
        successful reservation reseeds from DB.
        """
        db = MagicMock()
        with (
            patch.object(storage_quota_service, "get_cache", AsyncMock(return_value="0")),
            patch.object(
                storage_quota_service,
                "_atomic_check_and_incr",
                AsyncMock(side_effect=RedisError("script eval failed")),
            ),
        ):
            # Must not raise.
            await storage_quota_service.reserve_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=1024,
                quota_bytes=10_000,
                db=db,
            )

    @pytest.mark.asyncio
    async def test_cold_start_reseeds_from_db(self, workspace_id):
        """Redis miss → DB aggregate → set_cache (with TTL) → atomic Lua."""
        db_session = MagicMock()
        result = MagicMock()
        result.scalar_one = MagicMock(return_value=2048)
        db_session.execute = AsyncMock(return_value=result)

        with (
            patch.object(storage_quota_service, "get_cache", AsyncMock(return_value=None)),
            patch.object(storage_quota_service, "set_cache", AsyncMock()) as set_cache,
            patch.object(
                storage_quota_service,
                "_atomic_check_and_incr",
                AsyncMock(return_value=(3072, 2048)),
            ),
        ):
            await storage_quota_service.reserve_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=1024,
                quota_bytes=10_000,
                db=db_session,
            )
            set_cache.assert_awaited_once()
            args, kwargs = set_cache.call_args
            assert args[1] == "2048"
            assert kwargs.get("ttl") == 86400

    @pytest.mark.asyncio
    async def test_concurrent_reserve_second_call_loses(self, workspace_id):
        """Sequential proxy for concurrent reserve: when two callers
        compete at the cap, the Lua script makes the second one fail.

        Pre-#554 the GET → check → INCRBY race could let both succeed
        (over-commit). With the atomic Lua, the loser receives the -1
        sentinel and ``QuotaExceededError`` propagates."""
        db = MagicMock()
        with patch.object(
            storage_quota_service,
            "get_cache",
            AsyncMock(side_effect=["8000", "9000"]),
        ):
            # First call: seed=8000, Lua observes 8000 then INCRBY → 9000.
            with patch.object(
                storage_quota_service,
                "_atomic_check_and_incr",
                AsyncMock(return_value=(9000, 8000)),
            ):
                await storage_quota_service.reserve_storage_bytes(
                    workspace_id=workspace_id,
                    size_bytes=1000,
                    quota_bytes=10_000,
                    db=db,
                )

            # Second call: seed=9000, Lua observes 9000, cap-exceeded.
            with patch.object(
                storage_quota_service,
                "_atomic_check_and_incr",
                AsyncMock(return_value=(-1, 9000)),
            ):
                with pytest.raises(QuotaExceededError):
                    await storage_quota_service.reserve_storage_bytes(
                        workspace_id=workspace_id,
                        size_bytes=2000,
                        quota_bytes=10_000,
                        db=db,
                    )


class TestReleaseStorageBytes:
    @pytest.mark.asyncio
    async def test_release_calls_negative_incrby(self, workspace_id):
        with patch.object(
            storage_quota_service, "incrby_counter", AsyncMock(return_value=512)
        ) as incrby:
            await storage_quota_service.release_storage_bytes(
                workspace_id=workspace_id, size_bytes=512
            )
            incrby.assert_awaited_once()
            args, _ = incrby.call_args
            assert args[1] == -512

    @pytest.mark.asyncio
    async def test_release_zero_is_noop(self, workspace_id):
        with patch.object(storage_quota_service, "incrby_counter", AsyncMock()) as incrby:
            await storage_quota_service.release_storage_bytes(
                workspace_id=workspace_id, size_bytes=0
            )
            incrby.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_redis_error_swallowed(self, workspace_id):
        """Sweeper retries naturally; one failed release MUST NOT raise."""
        with patch.object(
            storage_quota_service,
            "incrby_counter",
            AsyncMock(side_effect=RedisError("down")),
        ):
            # Must not raise.
            await storage_quota_service.release_storage_bytes(
                workspace_id=workspace_id, size_bytes=512
            )

    @pytest.mark.asyncio
    async def test_release_underflow_does_not_raise(self, workspace_id):
        """Negative INCRBY result is logged but does not raise — net
        effect is "slightly under-counted", which is the safer drift
        direction (matches docstring contract)."""
        with patch.object(
            storage_quota_service,
            "incrby_counter",
            AsyncMock(return_value=-100),
        ):
            # Must not raise.
            await storage_quota_service.release_storage_bytes(
                workspace_id=workspace_id, size_bytes=500
            )


class TestGetCurrentStorageUsage:
    @pytest.mark.asyncio
    async def test_uses_redis_when_present(self, workspace_id):
        db = MagicMock()
        with patch.object(storage_quota_service, "get_cache", AsyncMock(return_value="4096")):
            assert await storage_quota_service.get_current_storage_usage(workspace_id, db) == 4096

    @pytest.mark.asyncio
    async def test_falls_back_to_db_on_redis_error(self, workspace_id):
        db_session = MagicMock()
        result = MagicMock()
        result.scalar_one = MagicMock(return_value=8192)
        db_session.execute = AsyncMock(return_value=result)

        with patch.object(
            storage_quota_service,
            "get_cache",
            AsyncMock(side_effect=RedisError("down")),
        ):
            assert (
                await storage_quota_service.get_current_storage_usage(workspace_id, db_session)
                == 8192
            )
