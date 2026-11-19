"""Routes that act on the caller's current workspace require its membership (#2928).

The authentication dependency puts a workspace-scoped key's own workspace in
``current_workspace_id``; these routes read that workspace and go through the
shared workspace access check first. On real PostgreSQL rows, over HTTP with
real API keys (and a browser session):

* ``GET /workspace/embedding-status`` answers a key scoped to workspace X
  whose user is not a member of X (and a session whose stored current
  workspace is X) with the uniform refusal;
* a member's key and a member's session keep the response they get today.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from auth.api_keys import APIKeyManager
from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, Context, ExternalAPIKey, User, Workspace, WorkspaceMember
from models.memory import Memory

pytestmark = pytest.mark.asyncio(loop_scope="session")

REFUSAL = {"error": "AUTH-101", "message": "Insufficient permissions", "details": {}}
FAILED_SUMMARY = "Shared runbook row whose embedding failed"


def _memory(ws: UUID, ctx: UUID, author: str, summary: str, status: str) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=ws,
        context_id=ctx,
        user_id=author,
        summary=summary,
        content="full content",
        type="note",
        importance=0.5,
        client="pytest",
        scope="working",
        embedding_status=status,
        embedding_error="Model not available" if status == "failed" else None,
    )


def _app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    from api.main import http_exception_handler, memory_cloud_exception_handler
    from api.routes import system, workspace
    from db.base import get_db
    from utils.exceptions import MemoryCloudException

    app = FastAPI()
    app.include_router(workspace.router, prefix="/api/v1")
    app.include_router(system.router, prefix="/api/v1")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]

    @app.middleware("http")
    async def browser_session(request: Request, call_next):
        user_id = request.headers.get("x-test-session")
        request.state.user = (
            {"user_id": user_id, "sub": user_id, "email": f"{user_id}@example.test"}
            if user_id
            else None
        )
        return await call_next(request)

    async def request_db():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = request_db
    return app


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine, monkeypatch, fernet_api_key_secret):
    """Workspace X (owner + member) with a shared context holding one embedded
    and one failed memory, and an enabled OpenAI key; an outsider whose own
    workspace is Y, holding a key row scoped to X."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, member, outsider = (f"cw-{role}-{tag}" for role in ("owner", "member", "outsider"))
    users = [owner, member, outsider]
    x, y, shared = uuid4(), uuid4(), uuid4()
    async with factory() as db:
        db.add_all(
            [User(user_id=uid, email=f"{uid}@example.test", name=uid, role="user") for uid in users]
        )
        await db.flush()
        db.add_all(
            [
                Workspace(id=x, name=f"x-{tag}", owner_user_id=owner, plan_name="promax"),
                Workspace(id=y, name=f"y-{tag}", owner_user_id=outsider, plan_name="promax"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                WorkspaceMember(workspace_id=x, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(
                    workspace_id=x,
                    user_id=member,
                    role=WorkspaceRole.MEMBER,
                    allowed_context_ids=[shared],
                ),
                WorkspaceMember(workspace_id=y, user_id=outsider, role=WorkspaceRole.OWNER),
                Context(
                    id=shared, workspace_id=x, name="shared", created_by=owner, is_private=False
                ),
                ExternalAPIKey(
                    key_name="openai-x",
                    provider="openai",
                    encrypted_value="unused",
                    user_id=owner,
                    workspace_id=x,
                    enabled=True,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                _memory(x, shared, owner, "Shared row that embedded", "success"),
                _memory(x, shared, owner, FAILED_SUMMARY, "failed"),
            ]
        )
        await db.execute(update(User).where(User.user_id == member).values(current_workspace_id=x))
        await db.execute(
            update(User).where(User.user_id == outsider).values(current_workspace_id=y)
        )
        manager = APIKeyManager(db)
        # Inserted directly: a key row scoped to X whose user is not a member of X.
        outsider_key, _ = await manager.create_key(
            name="outside-x", user_id=outsider, workspace_id=x
        )
        member_key, _ = await manager.create_key(name="member-x", user_id=member, workspace_id=x)
        await db.commit()
    try:
        yield SimpleNamespace(
            factory=factory,
            app=_app(factory),
            owner=owner,
            member=member,
            outsider=outsider,
            x=x,
            y=y,
            outsider_key=outsider_key,
            member_key=member_key,
        )
    finally:
        async with factory() as db:
            await db.execute(delete(Memory).where(Memory.workspace_id.in_([x, y])))
            await db.execute(delete(APIKey).where(APIKey.user_id.in_(users)))
            await db.execute(delete(ExternalAPIKey).where(ExternalAPIKey.workspace_id.in_([x, y])))
            await db.execute(
                update(User).where(User.user_id.in_(users)).values(current_workspace_id=None)
            )
            await db.execute(delete(Context).where(Context.workspace_id.in_([x, y])))
            await db.execute(
                delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_([x, y]))
            )
            await db.execute(delete(Workspace).where(Workspace.id.in_([x, y])))
            await db.execute(delete(User).where(User.user_id.in_(users)))
            await db.commit()


async def _get(world, path: str, *, bearer: str | None = None, session: str | None = None):
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if session:
        headers["x-test-session"] = session
    async with AsyncClient(
        transport=ASGITransport(app=world.app), base_url="https://testserver"
    ) as client:
        return await client.get(path, headers=headers)


async def _select_x_for(world, user_id: str) -> None:
    """Store X as the user's current workspace, as a stale selection would."""
    async with world.factory() as db:
        await db.execute(
            update(User).where(User.user_id == user_id).values(current_workspace_id=world.x)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# GET /workspace/embedding-status
# ---------------------------------------------------------------------------


async def test_embedding_status_requires_membership_of_the_current_workspace(world):
    """A key scoped to X whose user is not a member of X, and a session whose
    stored current workspace is X, get the uniform refusal."""
    by_key = await _get(world, "/api/v1/workspace/embedding-status", bearer=world.outsider_key)
    await _select_x_for(world, world.outsider)
    by_session = await _get(world, "/api/v1/workspace/embedding-status", session=world.outsider)

    assert [(r.status_code, r.json()) for r in (by_key, by_session)] == [(403, REFUSAL)] * 2


async def test_embedding_status_for_a_member_is_unchanged(world):
    for kwargs in ({"bearer": world.member_key}, {"session": world.member}):
        response = await _get(world, "/api/v1/workspace/embedding-status", **kwargs)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        assert body["by_status"] == {"success": 1, "failed": 1}
        assert [m["summary"] for m in body["failed_memories"]] == [FAILED_SUMMARY]
