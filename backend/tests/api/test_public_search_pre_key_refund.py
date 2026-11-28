"""A valid bound key does not spend the pre-key limit of its context (#3054).

``POST /api/v1/public/{context_id}/search`` counts every request that carries
``Authorization`` in the context's pre-key bucket before the key is verified
(it protects the key lookup). A request whose public-bound key verified and
whose own per-key bucket then admitted it gives that slot back; every other
path keeps it: an unknown key, a refused key, a request its own bucket
refused, and a request whose own bucket could not be read.

Driven directly with the fake session of ``test_public_search_results``; the
buckets are faked here, and on Redis in ``tests/db/test_window_slot.py`` and
``tests/integration/test_public_bound_key_endpoint.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routes import public_search as route
from api.routes.public_search import PublicSearchRequest, public_search
from db.redis import WindowSlot
from models.auth import Workspace
from tests.api.test_public_search_results import OWNER_ID, _context, _FakeDB
from utils.exceptions import (
    APIKeyError,
    AuthorizationError,
    RateLimitError,
    RedisError,
)


def _slot() -> WindowSlot:
    return WindowSlot(
        key="public_search_pre_auth:x:minute", count=1, expires_at_ms=1_800_000_000_000
    )


class _Run:
    """The route with its gates faked; ``order`` records the buckets' calls."""

    def __init__(self) -> None:
        self.context = _context()
        self.workspace = Workspace(
            id=self.context.workspace_id, name="w", plan_name="pro", owner_user_id=OWNER_ID
        )
        self.db = _FakeDB(context=self.context, workspace=self.workspace)
        self.order: list[str] = []
        self.slot: WindowSlot | None = _slot()
        self.key = SimpleNamespace(id=7, user_id=OWNER_ID, bound_context_id=self.context.id)
        self.attribution: AsyncMock = AsyncMock(return_value=self.key)
        self.bound_bucket: AsyncMock = AsyncMock(side_effect=self._admit)
        self.capacity: AsyncMock = AsyncMock()
        self.refund: AsyncMock = AsyncMock(side_effect=self._refund)
        search = MagicMock()
        search.return_value.hybrid_search = AsyncMock(return_value=[])
        self.search = search

    async def _admit(self, *_a: Any) -> bool:
        self.order.append("bound_bucket")
        return True

    async def _refund(self, slot: WindowSlot) -> None:
        self.order.append("refund")

    async def _pre_key(self, *_a: Any) -> WindowSlot | None:
        self.order.append("pre_key")
        return self.slot

    async def run(self, *, api_key: str | None = "kagura_bound") -> Any:
        with (
            patch.object(route, "check_pre_auth_rate_limit", AsyncMock(side_effect=self._pre_key)),
            patch.object(route, "_resolve_public_attribution", self.attribution),
            patch.object(route, "check_bound_key_rate_limit", self.bound_bucket),
            patch.object(route, "check_public_search_rate_limit", AsyncMock(return_value=True)),
            patch.object(route, "refund_pre_auth_slot", self.refund),
            patch.object(route, "public_usage_budget_limit", lambda: None),
            patch.object(route, "ensure_not_capacity_locked", self.capacity),
            patch.object(route, "ensure_slot_active", AsyncMock()),
            patch.object(route, "SearchService", self.search),
            patch.object(route, "log_usage", AsyncMock()),
        ):
            return await public_search(
                context_id=self.context.id,
                request=PublicSearchRequest(query="q"),
                api_key=api_key,
                db=self.db,  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_an_admitted_bound_key_gives_the_pre_key_slot_back() -> None:
    run = _Run()

    response = await run.run()

    assert response.status == "success"
    run.refund.assert_awaited_once_with(run.slot)
    assert run.order == ["pre_key", "bound_bucket", "refund"]


@pytest.mark.asyncio
async def test_two_keys_on_one_context_each_give_their_slot_back() -> None:
    run = _Run()
    other = SimpleNamespace(id=8, user_id=OWNER_ID, bound_context_id=run.context.id)

    await run.run()
    run.attribution.return_value = other
    await run.run()

    assert [call.args[0] for call in run.bound_bucket.await_args_list] == [7, 8]
    assert run.refund.await_count == 2
    assert run.order == ["pre_key", "bound_bucket", "refund"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [APIKeyError("Invalid or expired API key"), AuthorizationError("not bound")],
    ids=["unknown_key", "refused_key"],
)
async def test_an_unknown_or_refused_key_keeps_its_slot(refusal: Exception) -> None:
    run = _Run()
    run.attribution.side_effect = refusal

    with pytest.raises(type(refusal)):
        await run.run()

    run.refund.assert_not_awaited()
    run.bound_bucket.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["key_limit", "key_not_provisioned"])
async def test_a_request_its_own_bucket_refuses_keeps_its_slot(reason: str) -> None:
    run = _Run()
    run.bound_bucket.side_effect = RateLimitError("over", retry_after=60, reason=reason)

    with pytest.raises(RateLimitError) as exc:
        await run.run()

    assert exc.value.details["reason"] == reason
    run.refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_request_whose_own_bucket_was_not_read_keeps_its_slot() -> None:
    """The per-key bucket fails open when Redis cannot be read: the request is
    served, but no bucket counted it, so the pre-key slot stays."""
    run = _Run()
    run.bound_bucket.side_effect = None
    run.bound_bucket.return_value = False

    response = await run.run()

    assert response.status == "success"
    run.refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_slot_taken_means_nothing_to_give_back() -> None:
    """The pre-key bucket could not be read (it fails open): no slot."""
    run = _Run()
    run.slot = None

    await run.run()

    run.refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_serving_gate_after_admission_does_not_take_the_slot_again() -> None:
    """The slot is given back as soon as the key's own bucket admitted the
    request; a refusal of a later serving gate does not change that."""
    run = _Run()
    run.capacity.side_effect = AuthorizationError("paused")

    with pytest.raises(AuthorizationError):
        await run.run()

    run.refund.assert_awaited_once_with(run.slot)


@pytest.mark.asyncio
async def test_an_anonymous_search_takes_no_pre_key_slot() -> None:
    run = _Run()
    run.attribution.return_value = None

    await run.run(api_key=None)

    assert run.order == []
    run.refund.assert_not_awaited()


# ---------------------------------------------------------------------------
# The buckets themselves (Redis faked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pre_key_check_returns_the_slot_it_took() -> None:
    slot = _slot()
    take = AsyncMock(return_value=slot)
    with patch.object(route, "take_window_slot", take):
        assert await route.check_pre_auth_rate_limit(_context().id) is slot
    assert take.await_args.kwargs == {"ttl": 60}


@pytest.mark.asyncio
async def test_a_refused_pre_key_check_keeps_its_slot_and_names_its_limit() -> None:
    over = WindowSlot(key="k", count=201, expires_at_ms=1_800_000_000_000)
    with (
        patch.object(route, "take_window_slot", AsyncMock(return_value=over)),
        pytest.raises(RateLimitError) as exc,
    ):
        await route.check_pre_auth_rate_limit(_context().id)
    assert exc.value.details["reason"] == "pre_auth_limit"


@pytest.mark.asyncio
async def test_an_unreadable_pre_key_bucket_fails_open_without_a_slot() -> None:
    with patch.object(route, "take_window_slot", AsyncMock(side_effect=RedisError("down"))):
        assert await route.check_pre_auth_rate_limit(_context().id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("count", "counted"), [(1, True), (100, True)])
async def test_the_bound_bucket_says_it_counted(count: int, counted: bool) -> None:
    with patch.object(route, "incrby_counter", AsyncMock(return_value=count)):
        assert await route.check_bound_key_rate_limit(7, 100) is counted


@pytest.mark.asyncio
async def test_an_unreadable_bound_bucket_says_it_did_not_count() -> None:
    with patch.object(route, "incrby_counter", AsyncMock(side_effect=RedisError("down"))):
        assert await route.check_bound_key_rate_limit(7, 100) is False


@pytest.mark.asyncio
async def test_a_failed_refund_is_logged_and_swallowed() -> None:
    """A slot that cannot be given back stays spent: the safe direction."""
    with patch.object(route, "return_window_slot", AsyncMock(side_effect=RedisError("down"))):
        await route.refund_pre_auth_slot(_slot())


@pytest.mark.asyncio
async def test_a_refund_returns_the_slot() -> None:
    give_back = AsyncMock(return_value=True)
    slot = _slot()
    with patch.object(route, "return_window_slot", give_back):
        await route.refund_pre_auth_slot(slot)
    give_back.assert_awaited_once_with(slot)
