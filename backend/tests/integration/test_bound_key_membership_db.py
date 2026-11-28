"""A public-bound key lives only while its owner belongs to the context's workspace.

On real PostgreSQL rows:

* removing a member deletes the member's keys bound to contexts of that
  workspace, with the workspace-scoped ones (the predicate the credentials
  listing shows them by); the member's bound keys of other workspaces, other
  members' keys and the member's global key stay;
* a bound key whose owner has no membership row in the context's workspace,
  or whose workspace is soft-deleted, is refused by the REST public search
  and ``/info`` with an unknown key's 401, and the refusal writes nothing; a
  current member's key serves; MCP binding introspection lists neither;
* a soft-deleted context, and a context of a soft-deleted workspace, answer
  404 on both routes like a missing context, with a key and without;
* deleting a workspace's rows for good (admin user deletion, account
  erasure) deletes every key bound to its contexts: none stays without its
  binding;
* a soft-deleted context cannot be bound at mint.

Every row a test commits is deleted afterwards (``world`` fixture).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, or_, select, update

from api.main import app
from auth.api_keys import APIKeyManager
from models.auth import APIKey, AuditLog, Context, UsageStats, Workspace, WorkspaceMember
from models.operation_history import OperationHistory

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def world(connector_world, fernet_api_key_secret):
    """``connector_world`` plus the rows it does not clean up itself: the keys
    of its users (a global or bound key has no workspace to cascade from), the
    removal's history rows, the usage rows of the served searches and the
    ``workspace_purged`` audit rows of the admin deletion."""
    w = connector_world
    w.contexts = []
    yield w
    db = w.db
    await db.rollback()
    await db.execute(delete(APIKey).where(APIKey.user_id.in_(w.targets)))
    await db.execute(
        delete(UsageStats).where(
            or_(UsageStats.user_id.in_(w.targets), UsageStats.context_id.in_(w.contexts))
        )
    )
    await db.execute(
        delete(OperationHistory).where(OperationHistory.workspace_id.in_(w.workspace_ids))
    )
    await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(w.targets)))
    await db.commit()


async def _public_context(w, workspace: Workspace, creator: str) -> UUID:
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"pub-{uuid4().hex[:8]}",
        created_by=creator,
        is_public=True,
    )
    w.db.add(context)
    await w.db.flush()
    w.contexts.append(context.id)
    return context.id


async def _bound_key(w, user_id: str, context_id: UUID) -> tuple[str, int]:
    plaintext, row = await APIKeyManager(w.db).create_key(
        name=f"bound-{uuid4().hex[:6]}", user_id=user_id, bound_context_id=context_id
    )
    await w.db.flush()
    return plaintext, row.id


async def _surviving(w, ids) -> set[int]:
    w.db.expire_all()
    return set((await w.db.execute(select(APIKey.id).where(APIKey.id.in_(list(ids))))).scalars())


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def stub_search(monkeypatch):
    async def _stub(self, **kwargs):  # noqa: ARG001 - mirrors the caller
        return []

    monkeypatch.setattr("services.search_service.SearchService.hybrid_search", _stub)


async def _search(client: AsyncClient, context_id: UUID, plaintext: str | None):
    headers = {"Authorization": f"Bearer {plaintext}"} if plaintext else {}
    return await client.post(
        f"/api/v1/public/{context_id}/search", headers=headers, json={"query": "q", "limit": 3}
    )


async def _info(client: AsyncClient, context_id: UUID, plaintext: str | None):
    headers = {"Authorization": f"Bearer {plaintext}"} if plaintext else {}
    return await client.get(f"/api/v1/public/{context_id}/info", headers=headers)


# ---------------------------------------------------------------------------
# Member removal
# ---------------------------------------------------------------------------


async def test_removal_deletes_the_members_keys_bound_to_the_workspaces_contexts(world):
    w = world
    owner, member, other = await w.user("bk-owner"), await w.user("bk-member"), await w.user("bk-n")
    x = await w.workspace(owner, **{member: "member", other: "member"})
    y = await w.workspace(other, **{member: "member"})
    in_x = await _public_context(w, x, member)
    other_in_x = await _public_context(w, x, other)
    in_y = await _public_context(w, y, member)
    _, bound_x = await _bound_key(w, member, in_x)
    _, bound_y = await _bound_key(w, member, in_y)
    _, others_bound = await _bound_key(w, other, other_in_x)
    workspace_key = await _workspace_key(w, member, x)
    _, global_row = await APIKeyManager(w.db).create_key(name="bk-global", user_id=member)
    await w.db.flush()
    global_key = global_row.id

    await w.remove(x.id, member)

    keys = {bound_x, bound_y, others_bound, workspace_key, global_key}
    assert await _surviving(w, keys) == {bound_y, others_bound, global_key}


async def _workspace_key(w, user_id: str, workspace: Workspace) -> int:
    _, row = await APIKeyManager(w.db).create_key(
        name=f"ws-{uuid4().hex[:6]}", user_id=user_id, workspace_id=workspace.id
    )
    await w.db.flush()
    return row.id


async def test_removal_deletes_a_revoked_bound_key_too(world):
    """The listing hides revoked keys; the removal takes them with the rest,
    as it does a revoked workspace key."""
    w = world
    owner, member = await w.user("bk-owner"), await w.user("bk-member")
    x = await w.workspace(owner, **{member: "admin"})
    context_id = await _public_context(w, x, member)
    _, key_id = await _bound_key(w, member, context_id)
    await w.db.execute(update(APIKey).where(APIKey.id == key_id).values(revoked_at=func.now()))

    await w.remove(x.id, member)

    assert await _surviving(w, {key_id}) == set()


# ---------------------------------------------------------------------------
# The REST public search and /info
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def served(world):
    """Workspace X: a member with a bound key, a user with a bound key of X
    and no membership row in it, and Y, a soft-deleted
    workspace whose owner holds a bound key."""
    w = world
    owner, member, departed = (
        await w.user("bk-owner"),
        await w.user("bk-member"),
        await w.user("bk-departed"),
    )
    gone_owner = await w.user("bk-gone")
    x = await w.workspace(owner, **{member: "member", departed: "member"})
    gone = await w.workspace(gone_owner)
    member_ctx = await _public_context(w, x, member)
    departed_ctx = await _public_context(w, x, departed)
    gone_ctx = await _public_context(w, gone, gone_owner)
    member_key, _ = await _bound_key(w, member, member_ctx)
    departed_key, departed_id = await _bound_key(w, departed, departed_ctx)
    gone_key, _ = await _bound_key(w, gone_owner, gone_ctx)
    # A bound key whose owner has no membership row.
    await w.db.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == x.id, WorkspaceMember.user_id == departed
        )
    )
    await w.db.execute(
        update(Workspace).where(Workspace.id == gone.id).values(deleted_at=func.now())
    )
    await w.db.commit()
    return SimpleNamespace(
        db=w.db,
        member=(member_ctx, member_key),
        departed=(departed_ctx, departed_key),
        departed_id=departed_id,
        departed_public_id=(await w.db.get(APIKey, departed_id)).public_id,
        gone=(gone_ctx, gone_key),
        member_user=member,
        departed_user=departed,
        gone_user=gone_owner,
        x=x.id,
    )


async def test_a_current_members_bound_key_is_served(served, client, stub_search):
    context_id, plaintext = served.member

    response = await _search(client, context_id, plaintext)
    assert response.status_code == 200, response.text
    info = await _info(client, context_id, plaintext)
    assert info.status_code == 200, info.text


@pytest.mark.parametrize("case", ["departed", "gone"])
async def test_a_key_whose_owner_left_the_workspace_reads_as_unknown(
    served, client, stub_search, case
):
    context_id, plaintext = getattr(served, case)
    unknown = await _search(client, context_id, "kagura_" + "0" * 40)
    assert unknown.status_code == 401

    response = await _search(client, context_id, plaintext)
    assert response.status_code == 401, response.text
    assert response.json() == unknown.json()

    info = await _info(client, context_id, plaintext)
    assert info.status_code == 401, info.text
    assert info.json() == unknown.json()


async def test_a_refused_bound_key_writes_nothing(served, client, stub_search):
    context_id, plaintext = served.departed

    assert (await _search(client, context_id, plaintext)).status_code == 401

    served.db.expire_all()
    key = await served.db.get(APIKey, served.departed_id)
    assert key is not None and key.last_used_at is None
    rows = await served.db.scalar(
        select(func.count())
        .select_from(UsageStats)
        .where(UsageStats.api_key_id == served.departed_id)
    )
    assert rows == 0


@pytest.mark.parametrize("with_key", [False, True])
async def test_a_soft_deleted_context_answers_like_a_missing_one(
    served, client, stub_search, with_key
):
    context_id, plaintext = served.member
    await served.db.execute(
        update(Context).where(Context.id == context_id).values(deleted_at=func.now())
    )
    await served.db.commit()
    key = plaintext if with_key else None

    for call in (_search, _info):
        missing_id = uuid4()
        missing = await call(client, missing_id, None)
        response = await call(client, context_id, key)
        assert missing.status_code == 404
        assert response.status_code == 404, response.text
        assert response.text.replace(str(context_id), "ID") == missing.text.replace(
            str(missing_id), "ID"
        )


async def test_a_context_of_a_soft_deleted_workspace_answers_like_a_missing_one(
    served, client, stub_search
):
    context_id, _ = served.gone

    for call in (_search, _info):
        missing_id = uuid4()
        missing = await call(client, missing_id, None)
        response = await call(client, context_id, None)
        assert response.status_code == 404, response.text
        assert response.text.replace(str(context_id), "ID") == missing.text.replace(
            str(missing_id), "ID"
        )


# ---------------------------------------------------------------------------
# Deleting a workspace's rows for good
# ---------------------------------------------------------------------------


async def test_admin_user_deletion_deletes_the_keys_bound_to_its_workspaces(world):
    from services.user_deletion_service import delete_user_rows

    w = world
    owner, member, outsider = (
        await w.user("bk-owner"),
        await w.user("bk-member"),
        await w.user("bk-out"),
    )
    doomed = await w.workspace(owner, **{member: "member"})
    kept = await w.workspace(outsider, **{member: "member"})
    doomed_ctx = await _public_context(w, doomed, member)
    kept_ctx = await _public_context(w, kept, member)
    _, doomed_key = await _bound_key(w, member, doomed_ctx)
    _, kept_key = await _bound_key(w, member, kept_ctx)
    await w.db.execute(
        update(Workspace).where(Workspace.id == doomed.id).values(deleted_at=func.now())
    )
    await w.db.commit()

    await delete_user_rows(w.db, owner)
    await w.db.commit()

    assert await _surviving(w, {doomed_key, kept_key}) == {kept_key}


async def test_account_erasure_deletes_the_keys_bound_to_its_workspaces(world):
    w = world
    owner, member, outsider = (
        await w.user("bk-owner"),
        await w.user("bk-member"),
        await w.user("bk-out"),
    )
    doomed = await w.workspace(owner, **{member: "member"})
    kept = await w.workspace(outsider, **{member: "member"})
    doomed_ctx = await _public_context(w, doomed, member)
    kept_ctx = await _public_context(w, kept, member)
    _, doomed_key = await _bound_key(w, member, doomed_ctx)
    _, kept_key = await _bound_key(w, member, kept_ctx)
    # A live workspace with members is handed on before an erasure; a
    # soft-deleted one is deleted with its rows.
    await w.db.execute(
        update(Workspace).where(Workspace.id == doomed.id).values(deleted_at=func.now())
    )

    from tests.integration.test_erasure_lock_order_db import _external_stores_stubbed

    with _external_stores_stubbed():
        await w.erase(owner)

    assert await _surviving(w, {doomed_key, kept_key}) == {kept_key}


# ---------------------------------------------------------------------------
# MCP binding introspection
# ---------------------------------------------------------------------------


def _tool_data(result) -> dict:
    import json

    return json.loads(result[0].text)


async def test_binding_introspection_lists_only_bindings_whose_workspace_is_held(served):
    from mcp_server.tools.api_keys import handle_describe_binding, handle_list_my_bindings

    member_ctx, _ = served.member
    listed = _tool_data(await handle_list_my_bindings({}, served.member_user, None))
    assert [b["context_id"] for b in listed["bindings"]] == [str(member_ctx)]

    for case in ("departed", "gone"):
        user_id = getattr(served, f"{case}_user")
        context_id, _ = getattr(served, case)
        listed = _tool_data(await handle_list_my_bindings({}, user_id, None))
        assert listed["bindings"] == [], case
        by_context = _tool_data(
            await handle_describe_binding({"context_id": str(context_id)}, user_id, None)
        )
        assert by_context["error"] == "binding_not_found", case

    by_key = _tool_data(
        await handle_describe_binding(
            {"key_id": served.departed_public_id}, served.departed_user, None
        )
    )
    assert by_key["error"] == "binding_not_found"


# ---------------------------------------------------------------------------
# Minting against a soft-deleted context
# ---------------------------------------------------------------------------


async def test_a_soft_deleted_context_cannot_be_bound(world):
    from fastapi import BackgroundTasks, HTTPException

    from api.routes.member_credentials import create_api_key
    from models.schemas import CreateAPIKeyRequest

    w = world
    owner, member = await w.user("bk-owner"), await w.user("bk-member")
    x = await w.workspace(owner, **{member: "member"})
    x_id = x.id
    context_id = await _public_context(w, x, member)
    await w.db.execute(
        update(Context).where(Context.id == context_id).values(deleted_at=func.now())
    )
    await w.db.commit()

    with pytest.raises(ValueError, match="not found"):
        await APIKeyManager(w.db).create_key(
            name="bk-soft", user_id=member, bound_context_id=context_id
        )
    await w.db.rollback()

    with pytest.raises(HTTPException) as refused:
        await create_api_key(
            workspace_id=x_id,
            user_id=member,
            data=CreateAPIKeyRequest(name="bk-soft", bound_context_id=str(context_id)),
            request=SimpleNamespace(client=None, headers={}),  # type: ignore[arg-type]
            background_tasks=BackgroundTasks(),
            user={"user_id": member, "sub": member, "email": f"{member}@example.test"},
            db=w.db,
        )
    assert (refused.value.status_code, refused.value.detail) == (
        404,
        "bound_context_id: context not found",
    )
    await w.db.rollback()
    assert (
        await w.db.scalar(
            select(func.count()).select_from(APIKey).where(APIKey.bound_context_id == context_id)
        )
        == 0
    )
