"""Capacity-over lock predicate (#1941).

The COUNT / storage-row queries are stubbed: what is pinned here is the
predicate (who can be locked, which axis locks, how far over) and the error
it produces. The two reads are a plain indexed COUNT and a primary-key row.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.constants import GATE_CAPACITY, GATE_KINDS
from models.auth import ENTITLEMENT_SOURCE_ADMIN_GRANT, ENTITLEMENT_SOURCE_EXTERNAL_BILLING
from services import capacity_lock as cl
from utils.exceptions import CapacityLockedError

MB = 1024 * 1024


def _ws(
    plan_name: str = "free",
    source: str = ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    memory_limit: int = 1000,
    storage_limit: int = 100 * MB,
):
    return types.SimpleNamespace(
        id=uuid4(),
        plan_name=plan_name,
        entitlement_source=source,
        effective_memory_limit=memory_limit,
        effective_storage_limit_bytes=storage_limit,
    )


def _db(memory_count: int | None, used_bytes: int | None) -> MagicMock:
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[memory_count, used_bytes])
    return db


@pytest.fixture(autouse=True)
def _frontend_url():
    settings = types.SimpleNamespace(frontend_url="https://app.example.test/ ")
    with patch.object(cl, "get_settings", return_value=settings):
        yield


class TestWhoCanBeLocked:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("plan", ["basic", "pro", "promax"])
    async def test_a_paid_plan_is_never_locked_and_runs_no_query(self, plan: str) -> None:
        db = _db(10**6, 10**12)
        assert await cl.capacity_lock_state(db, _ws(plan_name=plan)) is None
        db.scalar.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_admin_managed_free_workspace_is_never_locked(self) -> None:
        """Self-hosted and admin-granted Free never came from a subscription."""
        db = _db(10**6, 10**12)
        ws = _ws(source=ENTITLEMENT_SOURCE_ADMIN_GRANT)
        assert await cl.capacity_lock_state(db, ws) is None
        db.scalar.assert_not_called()


class TestThePredicate:
    @pytest.mark.asyncio
    async def test_within_both_limits_is_not_locked(self) -> None:
        assert await cl.capacity_lock_state(_db(1000, 100 * MB), _ws()) is None

    @pytest.mark.asyncio
    async def test_a_missing_storage_row_reads_as_zero_bytes(self) -> None:
        assert await cl.capacity_lock_state(_db(5, None), _ws()) is None

    @pytest.mark.asyncio
    async def test_one_memory_over_locks(self) -> None:
        lock = await cl.capacity_lock_state(_db(1001, 0), _ws())
        assert lock is not None
        assert (lock.memory_count, lock.memory_limit, lock.over_memories) == (1001, 1000, 1)
        assert lock.over_bytes == 0

    @pytest.mark.asyncio
    async def test_storage_over_locks_on_its_own(self) -> None:
        lock = await cl.capacity_lock_state(_db(3, 100 * MB + 1), _ws())
        assert lock is not None
        assert lock.over_memories == 0
        assert lock.over_bytes == 1
        assert lock.used_bytes == 100 * MB + 1
        assert lock.storage_limit_bytes == 100 * MB

    @pytest.mark.asyncio
    async def test_the_cleanup_url_points_at_the_plan_page(self) -> None:
        lock = await cl.capacity_lock_state(_db(2000, 0), _ws())
        assert lock is not None
        assert lock.cleanup_url == "https://app.example.test/workspace/settings/plan"

    @pytest.mark.asyncio
    async def test_a_negative_limit_reads_as_unlimited(self) -> None:
        assert await cl.capacity_lock_state(_db(10**6, 0), _ws(memory_limit=-1)) is None


class TestEnsure:
    @pytest.mark.asyncio
    async def test_raises_the_capacity_error_with_the_numbers(self) -> None:
        db = _db(1200, 150 * MB)
        db.get = AsyncMock(return_value=_ws())
        with pytest.raises(CapacityLockedError) as exc:
            await cl.ensure_not_capacity_locked(db, uuid4())
        err = exc.value
        assert err.status_code == 403
        assert err.error_code == "CAPACITY-001"
        assert err.details["gate"] == GATE_CAPACITY
        assert err.details["over_memories"] == 200
        assert err.details["over_bytes"] == 50 * MB
        assert err.details["cleanup_url"].endswith("/workspace/settings/plan")
        assert "200 memories and 50 MB" in err.message
        assert "listing, deleting and export still work" in err.message

    @pytest.mark.asyncio
    async def test_an_unknown_workspace_passes(self) -> None:
        db = MagicMock()
        db.get = AsyncMock(return_value=None)
        db.scalar = AsyncMock()
        await cl.ensure_not_capacity_locked(db, uuid4())
        db.scalar.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_passes_without_a_query(self) -> None:
        db = MagicMock()
        db.get = AsyncMock()
        await cl.ensure_not_capacity_locked(db, None)
        db.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_context_variant_resolves_the_owning_workspace(self) -> None:
        ws = _ws()
        db = MagicMock()
        db.scalar = AsyncMock(return_value=ws)
        with patch.object(cl, "ensure_not_capacity_locked", AsyncMock()) as ensure:
            await cl.ensure_context_not_capacity_locked(db, uuid4())
        ensure.assert_awaited_once_with(db, ws)

    @pytest.mark.asyncio
    async def test_the_context_variant_passes_an_unknown_context(self) -> None:
        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)
        with patch.object(cl, "ensure_not_capacity_locked", AsyncMock()) as ensure:
            await cl.ensure_context_not_capacity_locked(db, uuid4())
        ensure.assert_not_awaited()


class TestTheErrorText:
    def test_capacity_is_a_gate_kind(self) -> None:
        assert GATE_CAPACITY in GATE_KINDS

    def test_megabytes_round_up(self) -> None:
        err = CapacityLockedError(
            memory_count=1,
            memory_limit=1,
            over_memories=0,
            used_bytes=0,
            storage_limit_bytes=0,
            over_bytes=1,
            cleanup_url="u",
        )
        assert "0.1 MB" in err.message
        assert "1 memory" not in err.message

    def test_the_mcp_help_names_what_still_works(self) -> None:
        err = CapacityLockedError(
            memory_count=1001,
            memory_limit=1000,
            over_memories=1,
            used_bytes=0,
            storage_limit_bytes=0,
            over_bytes=0,
            cleanup_url="https://app.example.test/workspace/settings/plan",
        )
        help_text = err.help_text
        assert "1 memory" in help_text
        for allowed in ("list", "forget", "delete_context", "delete_file", "export"):
            assert allowed in help_text
        assert "https://app.example.test/workspace/settings/plan" in help_text
