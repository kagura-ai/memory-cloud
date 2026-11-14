"""Anonymous REST public search shares the public usage budget (#2805).

When the operator switch is on, an anonymous search (no member session, no
bound key) of a context with a live public MCP publication is charged to the
context's workspace after its per-minute bucket, the plan pause and the
capacity check, and before the search runs. Members and bound keys are never
charged; a context without a live publication is not counted (the older REST
feature); with the switch off nothing changes.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes import public_search as route
from api.routes.public_search import PublicSearchRequest, public_search
from models.auth import Context, Workspace
from services.public_usage_budget import BudgetCharge, PublicUsageBudgetUnavailable
from utils.exceptions import (
    PublicUsageBudgetExhaustedError,
    PublicUsageBudgetUnavailableError,
    QuotaExceededError,
    RateLimitError,
    TemporarilyUnavailableError,
)

LIMIT = 25000


class _Stop(Exception):
    """Raised by the stubbed search: the request got past every gate."""


def _charge(admitted: bool, retry: int | None = None) -> BudgetCharge:
    return BudgetCharge(
        admitted=admitted,
        calls_used=LIMIT if not admitted else 7,
        period_started_at=datetime(2026, 11, 10, 8),
        period_seq=1,
        retry_after_seconds=retry,
    )


def _db(plan: str = "pro"):
    workspace_id = uuid4()
    context = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        is_public=True,
        resource_id=None,
        name="pub",
    )
    workspace = Workspace(id=workspace_id, name="w", plan_name=plan, owner_user_id="owner")

    async def _get(model, _id):
        return {Context: context, Workspace: workspace}[model]

    db = MagicMock()
    db.get = AsyncMock(side_effect=_get)
    return db, context


class _Rig:
    def __init__(self, *, limit: int | None = LIMIT, published: bool = True) -> None:
        self.limit = limit
        self.bucket = AsyncMock(return_value=True)
        self.published = AsyncMock(return_value=published)
        self.charge = AsyncMock(return_value=_charge(True))
        self.search = MagicMock()
        self.search.return_value.hybrid_search = AsyncMock(side_effect=_Stop())
        self.limit_reads = 0

    def _limit(self) -> int | None:
        self.limit_reads += 1
        return self.limit

    async def run(
        self,
        *,
        plan: str = "pro",
        user: dict | None = None,
        api_key: str | None = None,
        bound_key=None,
        capacity=None,
    ):
        db, context = _db(plan)
        self.db, self.context = db, context
        if user is not None and "current_workspace_id" not in user:
            user = {**user, "current_workspace_id": context.workspace_id}
        with (
            patch.object(route, "check_public_search_rate_limit", self.bucket),
            patch.object(route, "check_pre_auth_rate_limit", AsyncMock()),
            patch.object(route, "check_bound_key_rate_limit", AsyncMock()),
            patch.object(route, "_resolve_public_attribution", AsyncMock(return_value=bound_key)),
            patch.object(route, "_has_live_publication", self.published),
            patch.object(route, "public_usage_budget_limit", self._limit),
            patch.object(route, "charge_public_usage", self.charge),
            patch.object(route, "ensure_not_capacity_locked", capacity or AsyncMock()),
            patch.object(route, "SearchService", self.search),
            patch.object(route, "log_usage", AsyncMock()),
        ):
            return await public_search(
                context_id=context.id,
                request=PublicSearchRequest(query="q", limit=3),
                user=user,
                api_key=api_key,
                db=db,
            )


async def _reaches_search(rig: _Rig, **kwargs) -> None:
    # The route wraps search errors into a 500; reaching the stub proves the
    # request got past every gate.
    with pytest.raises(HTTPException) as exc:
        await rig.run(**kwargs)
    assert isinstance(exc.value.__cause__, _Stop)


@pytest.mark.asyncio
async def test_anonymous_published_search_is_charged_to_the_context_workspace() -> None:
    rig = _Rig()
    await _reaches_search(rig)
    rig.charge.assert_awaited_once_with(rig.context.workspace_id, limit=LIMIT, db=rig.db)
    rig.published.assert_awaited_once_with(rig.db, rig.context.id)
    assert rig.limit_reads == 1


@pytest.mark.asyncio
async def test_a_search_that_fails_after_the_charge_stays_counted() -> None:
    rig = _Rig()
    await _reaches_search(rig)
    assert rig.charge.await_count == 1


@pytest.mark.asyncio
async def test_exhausted_budget_is_429_with_a_capped_hint_and_no_count() -> None:
    rig = _Rig()
    rig.charge.return_value = _charge(False, retry=3600)
    with pytest.raises(PublicUsageBudgetExhaustedError) as exc:
        await rig.run()
    err = exc.value
    assert err.status_code == 429
    assert err.error_code == "usage_budget_exhausted"
    assert err.details == {"retry_after": 3600}
    assert err.response_headers == {"Retry-After": "3600"}
    assert int(err.response_headers["Retry-After"]) <= 3600
    assert str(LIMIT) not in err.message
    assert err.log_level == "warning"
    rig.search.return_value.hybrid_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_charge_failure_is_503_temporarily_unavailable() -> None:
    rig = _Rig()
    rig.charge.side_effect = PublicUsageBudgetUnavailable("down")
    with pytest.raises(PublicUsageBudgetUnavailableError) as exc:
        await rig.run()
    assert exc.value.status_code == 503
    assert exc.value.error_code == "temporarily_unavailable"
    assert exc.value.response_headers == {"Retry-After": "60"}
    assert exc.value.log_level == "warning"
    rig.search.return_value.hybrid_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_minute_bucket_refuses_without_charging() -> None:
    rig = _Rig()
    rig.bucket.return_value = False
    with pytest.raises(PublicUsageBudgetUnavailableError):
        await rig.run()
    rig.charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_minute_bucket_over_the_limit_is_rate_001_and_not_charged() -> None:
    rig = _Rig()
    rig.bucket.side_effect = RateLimitError("over", retry_after=60)
    with pytest.raises(RateLimitError) as exc:
        await rig.run()
    assert exc.value.error_code == "RATE-001"
    rig.charge.assert_not_awaited()
    rig.published.assert_not_awaited()


@pytest.mark.asyncio
async def test_anonymous_search_without_a_live_publication_is_not_charged() -> None:
    rig = _Rig(published=False)
    await _reaches_search(rig)
    rig.charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_bucket_without_a_publication_still_fails_open() -> None:
    rig = _Rig(published=False)
    rig.bucket.return_value = False
    await _reaches_search(rig)
    rig.charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_session_is_never_charged() -> None:
    rig = _Rig()
    await _reaches_search(rig, user={"user_id": "owner"})
    rig.charge.assert_not_awaited()
    rig.published.assert_not_awaited()
    assert rig.limit_reads == 0


@pytest.mark.asyncio
async def test_bound_key_is_never_charged() -> None:
    rig = _Rig()
    key = SimpleNamespace(id=11, user_id="owner", bound_context_id=None)
    await _reaches_search(rig, api_key="kagura_bound", bound_key=key)
    rig.charge.assert_not_awaited()
    rig.published.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_pause_refuses_before_the_charge() -> None:
    rig = _Rig()
    with pytest.raises(QuotaExceededError):
        await rig.run(plan="free")
    rig.charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_capacity_lock_refuses_before_the_charge() -> None:
    rig = _Rig()
    locked = AsyncMock(side_effect=TemporarilyUnavailableError())
    with pytest.raises(TemporarilyUnavailableError):
        await rig.run(capacity=locked)
    rig.charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_off_runs_no_exists_and_no_charge() -> None:
    rig = _Rig(limit=None)
    rig.bucket.return_value = False  # today's fail-open bucket stays fail-open
    await _reaches_search(rig)
    rig.published.assert_not_awaited()
    rig.charge.assert_not_awaited()
    assert rig.limit_reads == 1


@pytest.mark.asyncio
async def test_bucket_reports_whether_it_counted(monkeypatch) -> None:
    """``check_public_search_rate_limit``: True counted, False unreadable, raise over."""
    from db.redis import RedisError

    monkeypatch.setattr(route, "incrby_counter", AsyncMock(return_value=1))
    assert await route.check_public_search_rate_limit(uuid4(), None) is True
    monkeypatch.setattr(route, "incrby_counter", AsyncMock(side_effect=RedisError("down")))
    assert await route.check_public_search_rate_limit(uuid4(), None) is False
    monkeypatch.setattr(route, "incrby_counter", AsyncMock(return_value=51))
    with pytest.raises(RateLimitError):
        await route.check_public_search_rate_limit(uuid4(), None)
