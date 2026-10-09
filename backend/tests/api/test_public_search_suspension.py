"""Anonymous public serving pauses on a plan with no public allowance (#1939).

The context keeps ``is_public``; only serving stops, with the same
``api_public_daily`` refusal an authenticated caller already gets from the
rate-limit middleware on such a plan. Re-subscribing serves it again.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.public_search import (
    PublicSearchRequest,
    get_public_context_info,
    public_search,
)
from models.auth import Context, Workspace
from utils.exceptions import QuotaExceededError


class _Stop(Exception):
    """Raised by the stubbed search to prove the request got past the gate."""


def _db(plan: str):
    workspace_id = uuid4()
    context = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        is_public=True,
        resource_id=None,
        name="pub",
        display_name=None,
        description=None,
        created_at=None,
    )
    workspace = Workspace(id=workspace_id, name="w", plan_name=plan, owner_user_id="owner")

    async def _get(model, _id):
        return {Context: context, Workspace: workspace}[model]

    db = MagicMock()
    db.get = AsyncMock(side_effect=_get)
    return db, context


async def _search(plan: str):
    db, context = _db(plan)
    search = MagicMock()
    search.return_value.hybrid_search = AsyncMock(side_effect=_Stop())
    with (
        patch("api.routes.public_search.check_public_search_rate_limit", new=AsyncMock()),
        patch("api.routes.public_search.SearchService", search),
        patch("api.routes.public_search.log_usage", new=AsyncMock()),
    ):
        return await public_search(
            context_id=context.id,
            request=PublicSearchRequest(query="q", limit=3),
            user=None,
            api_key=None,
            db=db,
        )


@pytest.mark.asyncio
async def test_anonymous_search_refused_on_free():
    with pytest.raises(QuotaExceededError) as exc:
        await _search("free")
    assert exc.value.status_code == 429
    assert exc.value.details["quota_type"] == "api_public_daily"


@pytest.mark.asyncio
async def test_anonymous_search_served_on_pro():
    # The route wraps search errors into a 500 — reaching the stub at all is
    # what proves the request got past the plan gate.
    with pytest.raises(HTTPException) as exc:
        await _search("pro")
    assert isinstance(exc.value.__cause__, _Stop)


@pytest.mark.asyncio
async def test_anonymous_info_refused_on_free():
    db, context = _db("free")
    with pytest.raises(QuotaExceededError) as exc:
        await get_public_context_info(context_id=context.id, api_key=None, db=db)
    assert exc.value.details["quota_type"] == "api_public_daily"


@pytest.mark.asyncio
async def test_anonymous_info_served_on_pro():
    db, context = _db("pro")
    body = await get_public_context_info(context_id=context.id, api_key=None, db=db)
    assert body["is_public"] is True
