"""GET /api/v1/contexts/{context_id}/never-delivered-count (#3022).

The route resolves the context first (uniform 404, served under the capacity
lock), then runs one count with the private-context owner filter; the body
carries the threshold and the exempt tags the server applied.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes import contexts as routes
from auth.dependencies import require_session_auth
from config.constants import NEVER_DELIVERED_AGE_DAYS
from services.capacity_lock import CapacityPolicy
from utils.exceptions import NotFoundException

USER = {"user_id": "viewer-1"}


async def _call(*, private: bool, count: int = 3, exempt=frozenset({"z", "status:open"})):
    order: list[str] = []
    context = MagicMock(is_private=private)
    perm = MagicMock()

    async def resolve(**kwargs):
        order.append("resolve")
        return context

    perm.resolve_context_for_workspace_read = AsyncMock(side_effect=resolve)

    async def counter(db, **kwargs):
        order.append("count")
        return count

    count_mock = AsyncMock(side_effect=counter)
    db = MagicMock()
    context_id = uuid4()
    with (
        patch.object(routes, "PermissionService", return_value=perm),
        patch.object(routes, "count_never_delivered", count_mock),
        patch.object(routes, "exempt_tags", return_value=exempt),
    ):
        body = await routes.get_never_delivered_count(context_id=context_id, user=USER, db=db)
    return body, perm, count_mock, order, context_id


@pytest.mark.asyncio
async def test_resolver_first_with_the_capacity_lock_allowed():
    body, perm, count_mock, order, context_id = await _call(private=False)
    assert order == ["resolve", "count"]
    perm.resolve_context_for_workspace_read.assert_awaited_once_with(
        user_id="viewer-1",
        context_id=context_id,
        key_workspace_id=None,
        capacity=CapacityPolicy.ALLOWED,
    )
    assert body.model_dump() == {
        "count": 3,
        "older_than_days": NEVER_DELIVERED_AGE_DAYS,
        "exempt_tags": ["status:open", "z"],
    }


@pytest.mark.asyncio
async def test_a_shared_context_counts_without_an_owner_filter():
    _, _, count_mock, _, context_id = await _call(private=False)
    kwargs = count_mock.await_args.kwargs
    assert kwargs["context_id"] == context_id
    assert kwargs["owner_user_id"] is None
    assert kwargs["viewer_user_id"] == "viewer-1"
    assert kwargs["exempt"] == frozenset({"z", "status:open"})


@pytest.mark.asyncio
async def test_a_private_context_passes_the_caller_as_owner():
    _, _, count_mock, _, _ = await _call(private=True)
    assert count_mock.await_args.kwargs["owner_user_id"] == "viewer-1"


@pytest.mark.asyncio
async def test_an_empty_exempt_set_is_an_empty_list():
    body, *_ = await _call(private=False, exempt=frozenset())
    assert body.exempt_tags == []


@pytest.mark.asyncio
async def test_a_resolver_refusal_runs_no_count():
    perm = MagicMock()
    perm.resolve_context_for_workspace_read = AsyncMock(
        side_effect=NotFoundException("Context", "x")
    )
    count_mock = AsyncMock()
    with (
        patch.object(routes, "PermissionService", return_value=perm),
        patch.object(routes, "count_never_delivered", count_mock),
        pytest.raises(NotFoundException) as exc,
    ):
        await routes.get_never_delivered_count(context_id=uuid4(), user=USER, db=MagicMock())
    assert exc.value.status_code == 404
    count_mock.assert_not_awaited()


def test_the_route_is_session_only():
    (route,) = [
        r
        for r in routes.router.routes
        if getattr(r, "path", "") == "/contexts/{context_id}/never-delivered-count"
    ]
    assert route.methods == {"GET"}
    calls = {d.call for d in route.dependant.dependencies}
    assert require_session_auth in calls
