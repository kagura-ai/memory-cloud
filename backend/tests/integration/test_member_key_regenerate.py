"""Selected-row rotation stays inside the member/workspace scope (#2379)."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes.member_credentials import regenerate_api_key
from auth.api_keys import APIKeyManager
from models.auth import APIKey
from utils.datetime import utcnow


async def _rotate(world, workspace, owner, key_id=None, *, requester=None):
    return await regenerate_api_key(
        workspace.id,
        owner,
        SimpleNamespace(client=None, headers={}),
        BackgroundTasks(),
        {"user_id": requester or owner, "sub": requester or owner},
        world.db,
        key_id=key_id,
    )


@pytest.mark.asyncio
async def test_rotates_clicked_older_key_when_newest_is_connector(
    connector_world, fernet_api_key_secret
):
    world = connector_world
    owner = await world.user("rotate-owner")
    ws = await world.workspace(owner)
    first = await world.personal_key(owner, ws)
    clicked = await world.db.get(APIKey, first)
    clicked.created_at = utcnow() - timedelta(days=1)
    await world.connector(ws, owner)
    rows = list((await world.db.scalars(select(APIKey).where(APIKey.workspace_id == ws.id))).all())
    connector = next(row for row in rows if row.connector_id is not None)
    await world.db.commit()

    response = await _rotate(world, ws, owner, clicked.public_id)
    await world.db.refresh(clicked)
    await world.db.refresh(connector)
    replacement = await world.db.scalar(select(APIKey).where(APIKey.public_id == response.key_id))
    assert clicked.revoked_at is not None
    assert connector.revoked_at is None
    assert replacement.name == clicked.name
    assert replacement.user_id == owner
    assert replacement.workspace_id == ws.id
    assert replacement.id not in {clicked.id, connector.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["owner", "workspace", "revoked", "expired", "missing"])
async def test_selected_id_cannot_escape_scope(connector_world, fernet_api_key_secret, scope):
    world = connector_world
    owner = await world.user("rotate-owner")
    other = await world.user("rotate-other")
    ws = await world.workspace(owner, **{other: "member"})
    elsewhere = await world.workspace(owner)
    requested = await world.personal_key(owner, ws)
    untouched = await world.db.get(APIKey, requested)
    if scope == "owner":
        key_id = await world.personal_key(other, ws)
    elif scope == "workspace":
        key_id = await world.personal_key(owner, elsewhere)
    else:
        key_id = requested
    target = await world.db.get(APIKey, key_id)
    if scope == "revoked":
        target.revoked_at = utcnow()
    elif scope == "expired":
        target.expires_at = utcnow() - timedelta(seconds=1)
    await world.db.commit()
    public_id = target.public_id if scope != "missing" else "akey_" + "0" * 22
    before = set((await world.db.scalars(select(APIKey.id))).all())

    with pytest.raises(HTTPException) as exc:
        await _rotate(world, ws, owner, public_id)
    assert exc.value.status_code == 404
    await world.db.refresh(untouched)
    if scope != "revoked":
        assert untouched.revoked_at is None
    assert set((await world.db.scalars(select(APIKey.id))).all()) == before


@pytest.mark.asyncio
async def test_selected_connector_is_rejected_before_revocation(
    connector_world, fernet_api_key_secret
):
    world = connector_world
    owner = await world.user("rotate-owner")
    ws = await world.workspace(owner)
    await world.connector(ws, owner)
    key = await world.db.scalar(select(APIKey).where(APIKey.workspace_id == ws.id))
    await world.db.commit()

    with pytest.raises(HTTPException) as exc:
        await _rotate(world, ws, owner, key.public_id)
    assert exc.value.status_code == 422
    await world.db.refresh(key)
    assert key.revoked_at is None


@pytest.mark.asyncio
async def test_legacy_omitted_id_still_rotates_newest(connector_world, fernet_api_key_secret):
    world = connector_world
    owner = await world.user("rotate-owner")
    ws = await world.workspace(owner)
    older_id = await world.personal_key(owner, ws)
    older = await world.db.get(APIKey, older_id)
    older.created_at = utcnow() - timedelta(days=1)
    newer_id = await world.personal_key(owner, ws)
    newer = await world.db.get(APIKey, newer_id)
    await world.db.commit()

    await _rotate(world, ws, owner)
    await world.db.refresh(older)
    await world.db.refresh(newer)
    assert older.revoked_at is None
    assert newer.revoked_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("finite", [True, False])
async def test_rotation_preserves_agent_and_exact_expiry(
    connector_world, fernet_api_key_secret, finite
):
    from models.agent import Agent

    world = connector_world
    owner = await world.user("rotate-owner")
    ws = await world.workspace(owner)
    agent = Agent(workspace_id=ws.id, name="rotation-agent", owner_user_id=owner)
    world.db.add(agent)
    await world.db.flush()
    _, old = await APIKeyManager(world.db).create_key(
        name="bound-key", user_id=owner, workspace_id=ws.id, agent_id=agent.id
    )
    expiry = utcnow() + timedelta(hours=1, seconds=17) if finite else None
    old.expires_at = expiry
    await world.db.commit()

    response = await _rotate(world, ws, owner, old.public_id)
    replacement = await world.db.scalar(select(APIKey).where(APIKey.public_id == response.key_id))
    assert replacement.agent_id == agent.id
    assert replacement.expires_at == expiry


@pytest.mark.asyncio
async def test_another_member_cannot_rotate_owner_key(connector_world, fernet_api_key_secret):
    from utils.exceptions import AuthorizationError

    world = connector_world
    owner = await world.user("rotate-owner")
    member = await world.user("rotate-member")
    ws = await world.workspace(owner, **{member: "member"})
    old = await world.db.get(APIKey, await world.personal_key(owner, ws))
    await world.db.commit()
    with pytest.raises(AuthorizationError):
        await _rotate(world, ws, owner, old.public_id, requester=member)
    await world.db.refresh(old)
    assert old.revoked_at is None


@pytest.mark.asyncio
async def test_key_id_query_rejects_internal_and_malformed_ids(connector_world):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routes.member_credentials import router
    from auth.dependencies import require_session_auth
    from db.base import get_db

    app = FastAPI()
    app.include_router(router)

    async def session():
        return {"user_id": "member", "sub": "member"}

    async def database():
        yield connector_world.db

    app.dependency_overrides[require_session_auth] = session
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for key_id in ["123", "akey_bad", "skey_" + "0" * 22]:
            response = await client.post(
                "/api/v1/workspaces/11111111-1111-4111-8111-111111111111/"
                "members/member/credentials/api-key/regenerate",
                params={"key_id": key_id},
            )
            assert response.status_code == 422


@pytest.mark.asyncio
async def test_concurrent_selected_rotation_mints_only_one_replacement(
    connector_world, fernet_api_key_secret, monkeypatch
):
    world = connector_world
    owner = await world.user("rotate-owner")
    ws = await world.workspace(owner)
    old = await world.db.get(APIKey, await world.personal_key(owner, ws))
    public_id = old.public_id
    workspace_id = ws.id
    await world.db.commit()
    maker = async_sessionmaker(world.db.bind, expire_on_commit=False)
    first_minted = asyncio.Event()
    release_first = asyncio.Event()
    real_create = APIKeyManager.create_key

    async def pause_after_mint(manager, *args, **kwargs):
        result = await real_create(manager, *args, **kwargs)
        first_minted.set()
        await release_first.wait()
        return result

    monkeypatch.setattr(APIKeyManager, "create_key", pause_after_mint)
    async with maker() as first_db, maker() as second_db:
        second_pid = await second_db.scalar(text("SELECT pg_backend_pid()"))
        first = asyncio.create_task(_rotate(SimpleNamespace(db=first_db), ws, owner, public_id))
        await asyncio.wait_for(first_minted.wait(), timeout=10)
        second = asyncio.create_task(_rotate(SimpleNamespace(db=second_db), ws, owner, public_id))
        try:
            # Wait for PostgreSQL to confirm actual row-lock contention.
            async with asyncio.timeout(10):
                while not await world.db.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": second_pid}
                ):
                    await asyncio.sleep(0.01)
            release_first.set()
            await asyncio.wait_for(first, timeout=10)
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(second, timeout=10)
            assert exc.value.status_code == 404
        finally:
            release_first.set()
            await asyncio.gather(first, second, return_exceptions=True)
    world.db.expire_all()
    rows = list(
        (await world.db.scalars(select(APIKey).where(APIKey.workspace_id == workspace_id))).all()
    )
    assert len(rows) == 2
    assert sum(row.revoked_at is None for row in rows) == 1


@pytest.mark.asyncio
async def test_inactive_agent_refuses_rotation_without_committing_revocation(
    connector_world, fernet_api_key_secret
):
    from models.agent import Agent
    from utils.exceptions import BadRequestError

    world = connector_world
    owner = await world.user("rotate-owner")
    ws = await world.workspace(owner)
    agent = Agent(workspace_id=ws.id, name="inactive-agent", owner_user_id=owner)
    world.db.add(agent)
    await world.db.flush()
    _, old = await APIKeyManager(world.db).create_key(
        name="bound-key", user_id=owner, workspace_id=ws.id, agent_id=agent.id
    )
    agent.status = "suspended"
    public_id = old.public_id
    await world.db.commit()
    with pytest.raises(BadRequestError) as exc:
        await _rotate(world, ws, owner, public_id)
    assert exc.value.status_code == 400
    await world.db.rollback()
    await world.db.refresh(old)
    assert old.revoked_at is None
