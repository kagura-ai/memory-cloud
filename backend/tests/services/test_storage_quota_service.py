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
        """Reservation within ceiling → INCRBY by size_bytes, no exception."""
        db = MagicMock()
        with (
            patch.object(storage_quota_service, "get_cache", AsyncMock(return_value="0")),
            patch.object(
                storage_quota_service, "incrby_counter", AsyncMock(return_value=1024)
            ) as incrby,
        ):
            await storage_quota_service.reserve_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=1024,
                quota_bytes=10_000,
                db=db,
            )
            incrby.assert_awaited_once()
            args, _ = incrby.call_args
            assert args[1] == 1024

    @pytest.mark.asyncio
    async def test_at_ceiling_raises_quota_exceeded(self, workspace_id):
        """current + size > quota_bytes → QuotaExceededError, no INCRBY."""
        db = MagicMock()
        with (
            patch.object(storage_quota_service, "get_cache", AsyncMock(return_value="9500")),
            patch.object(storage_quota_service, "incrby_counter", AsyncMock()) as incrby,
        ):
            with pytest.raises(QuotaExceededError, match="Storage quota exceeded"):
                await storage_quota_service.reserve_storage_bytes(
                    workspace_id=workspace_id,
                    size_bytes=1000,
                    quota_bytes=10_000,
                    db=db,
                )
            incrby.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quota_zero_disables_check(self, workspace_id):
        """quota_bytes <= 0 → skip the Redis round-trip entirely."""
        db = MagicMock()
        with (
            patch.object(storage_quota_service, "get_cache", AsyncMock()) as get_cache,
            patch.object(storage_quota_service, "incrby_counter", AsyncMock()) as incrby,
        ):
            await storage_quota_service.reserve_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=1024,
                quota_bytes=0,
                db=db,
            )
            get_cache.assert_not_awaited()
            incrby.assert_not_awaited()

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
    async def test_redis_error_is_swallowed_fail_open(self, workspace_id):
        """RedisError on get_cache MUST NOT block the upload."""
        db = MagicMock()
        with (
            patch.object(
                storage_quota_service,
                "get_cache",
                AsyncMock(side_effect=RedisError("redis down")),
            ),
            patch.object(storage_quota_service, "incrby_counter", AsyncMock()) as incrby,
        ):
            # No exception should bubble up; reservation simply skipped.
            await storage_quota_service.reserve_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=1024,
                quota_bytes=10_000,
                db=db,
            )
            incrby.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cold_start_reseeds_from_db(self, workspace_id):
        """Redis miss → DB aggregate → set_cache (with TTL) → INCRBY."""
        db_session = MagicMock()
        result = MagicMock()
        result.scalar_one = MagicMock(return_value=2048)
        db_session.execute = AsyncMock(return_value=result)

        with (
            patch.object(storage_quota_service, "get_cache", AsyncMock(return_value=None)),
            patch.object(storage_quota_service, "set_cache", AsyncMock()) as set_cache,
            patch.object(storage_quota_service, "incrby_counter", AsyncMock(return_value=3072)),
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
