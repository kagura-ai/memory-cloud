"""#3054, on Postgres and Redis: a valid bound key does not spend the pre-key limit.

``POST /api/v1/public/{ctx}/search`` over HTTP (the app, the request session,
the real key verification and the live Redis buckets). The pre-key limit of
the context (requests with ``Authorization``, counted before the key is
verified) is lowered to 2 so a few requests reach it. A request whose
public-bound key verified and whose own per-key bucket admitted it gives its
pre-key slot back; an unknown key, a refused key and a request its own bucket
refused keep theirs. Two keys on one context: neither spends the other's
allowance, and only unknown keys can use the pre-key limit up, for everyone
with a key.

The search itself is stubbed. Every row the test committed is deleted by its
own ids afterwards, and so are the Redis keys it used.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from api.main import app
from api.routes import public_search as route
from auth.api_keys import APIKeyManager
from auth.workspace_roles import WorkspaceRole
from db.redis import get_redis_client
from models.auth import APIKey, Context, UsageStats, User, Workspace, WorkspaceMember
from utils.datetime import utcnow

from ._admin_helpers import make_user, make_workspace

_PRE_KEY_LIMIT = 2


class _World:
    def __init__(self, db) -> None:
        self.db = db
        self.users: list[str] = []
        self.workspaces: list[UUID] = []
        self.contexts: list[UUID] = []
        self.redis_keys: list[str] = []

    async def public_context(self) -> tuple[Context, str]:
        user = make_user()
        self.db.add(user)
        await self.db.flush()
        ws = make_workspace(owner_user_id=user.user_id, plan_name="pro")
        self.db.add(ws)
        await self.db.flush()
        self.db.add(
            WorkspaceMember(workspace_id=ws.id, user_id=user.user_id, role=WorkspaceRole.OWNER)
        )
        ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"pub-{uuid4().hex[:8]}",
            created_by=user.user_id,
            is_private=False,
            is_public=True,
        )
        self.db.add(ctx)
        await self.db.commit()
        self.users.append(user.user_id)
        self.workspaces.append(ws.id)
        self.contexts.append(ctx.id)
        self.redis_keys += [
            f"public_search_pre_auth:{ctx.id}:minute",
            f"public_search:{ctx.id}:minute",
        ]
        await get_redis_client().delete(*self.redis_keys[-2:])
        return ctx, user.user_id

    async def key(self, user_id: str, **binding: Any) -> tuple[str, int]:
        plaintext, row = await APIKeyManager(self.db).create_key(
            name=f"k-{uuid4().hex[:6]}", user_id=user_id, **binding
        )
        await self.db.commit()
        self.redis_keys.append(f"public_bound_key:{row.id}:minute")
        await get_redis_client().delete(self.redis_keys[-1])
        return plaintext, row.id

    async def cleanup(self) -> None:
        await self.db.rollback()
        await self.db.execute(delete(UsageStats).where(UsageStats.context_id.in_(self.contexts)))
        await self.db.execute(delete(APIKey).where(APIKey.user_id.in_(self.users)))
        await self.db.execute(delete(Context).where(Context.id.in_(self.contexts)))
        await self.db.execute(
            delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(self.workspaces))
        )
        await self.db.execute(delete(Workspace).where(Workspace.id.in_(self.workspaces)))
        await self.db.execute(delete(User).where(User.user_id.in_(self.users)))
        await self.db.commit()
        if self.redis_keys:
            await get_redis_client().delete(*self.redis_keys)


@pytest_asyncio.fixture
async def world(db_session, monkeypatch, fernet_api_key_secret):
    original = route.check_pre_auth_rate_limit

    async def lowered(context_id, limit_per_minute: int = _PRE_KEY_LIMIT):
        return await original(context_id, limit_per_minute=_PRE_KEY_LIMIT)

    monkeypatch.setattr(route, "check_pre_auth_rate_limit", lowered)

    async def no_hits(self, **_kwargs):  # noqa: ARG001
        return []

    monkeypatch.setattr("services.search_service.SearchService.hybrid_search", no_hits)
    w = _World(db_session)
    try:
        yield w
    finally:
        await w.cleanup()


async def _search(ctx_id: UUID, key: str | None) -> Any:
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            f"/api/v1/public/{ctx_id}/search", headers=headers, json={"query": "hello"}
        )


async def _count(key: str) -> int:
    raw = await get_redis_client().get(key)
    return int(raw) if raw is not None else 0


def _reason(response: Any) -> str | None:
    return (response.json().get("details") or {}).get("reason")


@pytest.mark.asyncio
async def test_two_keys_on_one_context_do_not_spend_its_pre_key_limit(world):
    """Five searches with two valid keys, past a pre-key limit of 2: all are
    served, each counted in its own key's bucket, none left in the pre-key
    bucket."""
    ctx, owner = await world.public_context()
    key_a, id_a = await world.key(owner, bound_context_id=ctx.id)
    key_b, id_b = await world.key(owner, bound_context_id=ctx.id)

    statuses = [(await _search(ctx.id, key)).status_code for key in (key_a, key_a, key_b) * 2]

    assert statuses == [200] * 6
    assert await _count(f"public_search_pre_auth:{ctx.id}:minute") == 0
    assert await _count(f"public_bound_key:{id_a}:minute") == 4
    assert await _count(f"public_bound_key:{id_b}:minute") == 2


@pytest.mark.asyncio
async def test_unknown_keys_still_use_up_the_pre_key_limit_for_every_key(world):
    """The protection of the key lookup is unchanged: unknown keys spend the
    limit, and once they used it up a valid key is refused before its lookup,
    with the pre-key reason, and keeps that slot too."""
    ctx, owner = await world.public_context()
    key_a, id_a = await world.key(owner, bound_context_id=ctx.id)

    unknown = [await _search(ctx.id, f"kagura_unknown_{i}") for i in range(3)]
    valid = await _search(ctx.id, key_a)

    assert [r.status_code for r in unknown] == [401, 401, 429]
    assert _reason(unknown[2]) == "pre_auth_limit"
    assert valid.status_code == 429
    assert _reason(valid) == "pre_auth_limit"
    assert await _count(f"public_search_pre_auth:{ctx.id}:minute") == 4
    assert await _count(f"public_bound_key:{id_a}:minute") == 0


@pytest.mark.asyncio
async def test_a_refused_key_keeps_its_slot(world):
    """A key bound to another context (403), an owner-scoped key (403) and a
    revoked key (401) verify or fail before any per-key bucket: each keeps its
    pre-key slot."""
    ctx, owner = await world.public_context()
    other, other_owner = await world.public_context()
    elsewhere, _ = await world.key(other_owner, bound_context_id=other.id)
    unbound, _ = await world.key(owner)
    revoked, revoked_id = await world.key(owner, bound_context_id=ctx.id)
    row = await world.db.get(APIKey, revoked_id)
    row.revoked_at = utcnow()
    await world.db.commit()

    statuses = [(await _search(ctx.id, key)).status_code for key in (elsewhere, unbound)]
    assert statuses == [403, 403]
    assert await _count(f"public_search_pre_auth:{ctx.id}:minute") == 2
    response = await _search(ctx.id, revoked)
    assert response.status_code == 429  # the third request with a key
    assert _reason(response) == "pre_auth_limit"
    assert await _count(f"public_search_pre_auth:{ctx.id}:minute") == 3


@pytest.mark.asyncio
async def test_a_request_its_own_bucket_refuses_keeps_its_slot(world):
    """The key's own bucket is full: the request is refused with the key's
    reason, and its pre-key slot stays spent."""
    ctx, owner = await world.public_context()
    key_a, id_a = await world.key(owner, bound_context_id=ctx.id)
    await get_redis_client().set(f"public_bound_key:{id_a}:minute", 100, ex=60)

    response = await _search(ctx.id, key_a)

    assert response.status_code == 429
    assert _reason(response) == "key_limit"
    assert await _count(f"public_search_pre_auth:{ctx.id}:minute") == 1
