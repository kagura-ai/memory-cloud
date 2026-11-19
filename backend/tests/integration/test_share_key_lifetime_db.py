"""A share key never outlives its creator's right to share the context (#2930).

On real PostgreSQL rows, over HTTP with the share-key and session dependencies:

* a share key is honoured while the account that created it holds OWNER-level
  access to the bound context, the rule minting applies: on a shared context
  the workspace owner or an admin, or a member holding the context's owner role
  whose access list reaches it; on a private context only its creator (or an
  account linked to the creator). When the creator leaves the workspace, loses
  that role, loses the context from their access list or has no plan seat, or
  the context or its workspace is deleted, both share surfaces answer the
  uniform 401, byte-identical to an unknown key's, and record no use of the key;
* removing a member revokes that member's share keys for the workspace's
  contexts (a key of the same account in another workspace stays), with one
  ``credential.revoked`` record per key, idempotently; deleting or erasing the
  account deletes its share keys, as it deletes its API keys;
* a caller with OWNER-level access to a context revokes and lists every share
  key bound to it; anyone else gets the uniform 404. The list applies the slot
  gate after that decision; revoking is never blocked by it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from auth.share_keys import ShareKeyManager
from auth.workspace_roles import ContextRole, WorkspaceRole
from models.auth import Context, ContextMember, ShareKey, Workspace, WorkspaceMember
from models.operation_history import OperationHistory
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

UNIFORM_401 = {"error": "HTTP-401", "message": "Invalid or missing share key", "details": {}}
NOT_FOUND = {"error": "RES-001", "message": "Share key not found", "details": {}}
CONTEXT_NOT_FOUND = {"error": "RES-001", "message": "Context not found", "details": {}}


def _app(factory) -> FastAPI:
    from sqlalchemy.exc import SQLAlchemyError

    from api.main import (
        http_exception_handler,
        memory_cloud_exception_handler,
        sqlalchemy_exception_handler,
    )
    from api.routes import share_keys
    from db.base import get_db
    from utils.exceptions import MemoryCloudException

    app = FastAPI()
    app.include_router(share_keys.router, prefix="/api/v1")
    app.include_router(share_keys.recall_router, prefix="/api/v1")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)  # type: ignore[arg-type]

    @app.middleware("http")
    async def browser_session(request: Request, call_next):
        user_id = request.headers.get("x-test-session")
        request.state.user = {"user_id": user_id} if user_id else None
        return await call_next(request)

    async def request_db():
        async with factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = request_db
    return app


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine, db_session: AsyncSession, connector_world):
    """Workspace X: owner, admin, a member owning context C, a plain member; and
    workspace Y where the same member owns context D."""
    w = connector_world
    owner = await w.user("sk-owner")
    admin = await w.user("sk-admin")
    member = await w.user("sk-member")
    plain = await w.user("sk-plain")
    stranger = await w.user("sk-stranger")
    viewer = await w.user("sk-viewer")
    x = await w.workspace(
        owner,
        **{
            admin: WorkspaceRole.ADMIN,
            member: WorkspaceRole.MEMBER,
            plain: WorkspaceRole.MEMBER,
            viewer: WorkspaceRole.VIEWER,
        },
    )
    y = await w.workspace(stranger, **{member: WorkspaceRole.MEMBER})
    c, d, p = uuid4(), uuid4(), uuid4()
    db_session.add_all(
        [
            # A private context of the member's own (inserted directly).
            Context(
                id=p,
                workspace_id=x.id,
                name=f"sk-p-{p.hex[:6]}",
                created_by=member,
                is_private=True,
            ),
            Context(
                id=c,
                workspace_id=x.id,
                name=f"sk-c-{c.hex[:6]}",
                created_by=member,
                is_private=False,
            ),
            Context(
                id=d,
                workspace_id=y.id,
                name=f"sk-d-{d.hex[:6]}",
                created_by=member,
                is_private=False,
            ),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            ContextMember(context_id=c, user_id=member, role=ContextRole.OWNER),
            ContextMember(context_id=c, user_id=plain, role=ContextRole.EDITOR),
            ContextMember(context_id=d, user_id=member, role=ContextRole.OWNER),
        ]
    )
    for ws, uid, ctxs in ((x, member, [c, p]), (x, plain, [c]), (y, member, [d])):
        await db_session.execute(
            update(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == ws.id, WorkspaceMember.user_id == uid)
            .values(allowed_context_ids=ctxs)
        )
    manager = ShareKeyManager(db_session)
    keys = {}
    for label, creator, ctx in (
        ("member", member, c),
        ("admin", admin, c),
        ("owner", owner, c),
        ("member_y", member, d),
        ("private", member, p),
    ):
        plaintext, row = await manager.create_key(
            name=f"sk-{label}", user_id=creator, context_id=ctx
        )
        keys[label] = SimpleNamespace(plaintext=plaintext, public_id=row.public_id, id=row.id)
    await db_session.commit()
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    app = _app(factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield SimpleNamespace(
            db=db_session,
            client=client,
            app=app,
            engine=async_engine,
            owner=owner,
            admin=admin,
            member=member,
            plain=plain,
            stranger=stranger,
            viewer=viewer,
            x=SimpleNamespace(id=x.id),
            y=SimpleNamespace(id=y.id),
            c=c,
            d=d,
            p=p,
            keys=keys,
        )
    await db_session.rollback()
    await db_session.execute(
        delete(OperationHistory).where(OperationHistory.context_id.in_([c, d, p]))
    )
    await db_session.execute(delete(Context).where(Context.id.in_([c, d, p])))
    await db_session.commit()


async def _sessions(world, plaintext: str):
    return await world.client.get(
        "/api/v1/share/sessions", headers={"Authorization": f"Bearer {plaintext}"}
    )


async def _recall(world, plaintext: str):
    return await world.client.post(
        "/api/v1/share/recall",
        json={"query": "anything"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )


async def _refused_like_an_unknown_key(world, plaintext: str) -> None:
    unknown = await _sessions(world, "kagura_sk_" + "u" * 43)
    for response in (await _sessions(world, plaintext), await _recall(world, plaintext)):
        assert response.status_code == 401
        assert response.content == unknown.content
        assert response.json() == UNIFORM_401


async def _revoked_at(world, key_id: int):
    world.db.expire_all()
    return await world.db.scalar(select(ShareKey.revoked_at).where(ShareKey.id == key_id))


async def _last_used_at(world, key_id: int):
    world.db.expire_all()
    return await world.db.scalar(select(ShareKey.last_used_at).where(ShareKey.id == key_id))


async def _revocations(world, public_id: str) -> list[OperationHistory]:
    return list(
        (
            await world.db.execute(
                select(OperationHistory).where(
                    OperationHistory.operation == "credential.revoked",
                    OperationHistory.target_id == public_id,
                )
            )
        ).scalars()
    )


# ---------------------------------------------------------------------------
# The creator rule, per request
# ---------------------------------------------------------------------------


async def test_a_key_is_honoured_while_its_creator_can_share_the_context(world) -> None:
    for label in ("member", "admin", "owner", "member_y", "private"):
        response = await _sessions(world, world.keys[label].plaintext)
        assert response.status_code == 200, label


async def test_a_creator_who_left_the_workspace_ends_the_key(world) -> None:
    await world.db.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == world.x.id, WorkspaceMember.user_id == world.member
        )
    )
    await world.db.commit()
    await _refused_like_an_unknown_key(world, world.keys["member"].plaintext)
    # The same account's key in the workspace it belongs to keeps working.
    assert (await _sessions(world, world.keys["member_y"].plaintext)).status_code == 200


async def test_a_creator_whose_context_role_was_lowered_ends_the_key(world) -> None:
    await world.db.execute(
        update(ContextMember)
        .where(ContextMember.context_id == world.c, ContextMember.user_id == world.member)
        .values(role=ContextRole.EDITOR)
    )
    await world.db.commit()
    await _refused_like_an_unknown_key(world, world.keys["member"].plaintext)
    # Restored: the rule is read per request, nothing was stored.
    await world.db.execute(
        update(ContextMember)
        .where(ContextMember.context_id == world.c, ContextMember.user_id == world.member)
        .values(role=ContextRole.OWNER)
    )
    await world.db.commit()
    assert (await _sessions(world, world.keys["member"].plaintext)).status_code == 200


async def test_a_creator_who_lost_the_context_from_the_access_list_ends_the_key(world) -> None:
    await world.db.execute(
        update(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == world.x.id, WorkspaceMember.user_id == world.member)
        .values(allowed_context_ids=[])
    )
    await world.db.commit()
    await _refused_like_an_unknown_key(world, world.keys["member"].plaintext)


async def test_an_admin_creator_demoted_to_member_ends_the_key(world) -> None:
    await world.db.execute(
        update(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == world.x.id, WorkspaceMember.user_id == world.admin)
        .values(role=WorkspaceRole.MEMBER)
    )
    await world.db.commit()
    await _refused_like_an_unknown_key(world, world.keys["admin"].plaintext)


async def test_a_deleted_context_ends_the_key_with_the_same_answer(world) -> None:
    await world.db.execute(update(Context).where(Context.id == world.c).values(deleted_at=utcnow()))
    await world.db.commit()
    await _refused_like_an_unknown_key(world, world.keys["owner"].plaintext)


@pytest.mark.parametrize("label", ["owner", "member"])
async def test_a_deleted_workspace_ends_the_key_with_the_same_answer(world, label) -> None:
    await world.db.execute(
        update(Workspace).where(Workspace.id == world.x.id).values(deleted_at=utcnow())
    )
    await world.db.commit()
    await _refused_like_an_unknown_key(world, world.keys[label].plaintext)


async def test_a_creator_without_a_plan_seat_ends_the_key_until_seated_again(world) -> None:
    # One seat: the owner keeps it, the member creator has none.
    await world.db.execute(
        update(Workspace).where(Workspace.id == world.x.id).values(plan_name="free")
    )
    await world.db.commit()
    await _refused_like_an_unknown_key(world, world.keys["member"].plaintext)
    assert (await _sessions(world, world.keys["owner"].plaintext)).status_code == 200
    await world.db.execute(
        update(Workspace).where(Workspace.id == world.x.id).values(plan_name="promax")
    )
    await world.db.commit()
    assert (await _sessions(world, world.keys["member"].plaintext)).status_code == 200


def _share_key_writes(recorder) -> list[str]:
    return [s for s in recorder.statements if s.upper().startswith("UPDATE SHARE_KEYS")]


async def test_a_refused_request_records_no_use_of_the_key(world) -> None:
    from tests.integration._slot_world import StatementRecorder

    await world.db.execute(
        update(ContextMember)
        .where(ContextMember.context_id == world.c, ContextMember.user_id == world.member)
        .values(role=ContextRole.EDITOR)
    )
    await world.db.commit()
    with StatementRecorder(world.engine) as recorder:
        await _refused_like_an_unknown_key(world, world.keys["member"].plaintext)
    # No write at all, so no row lock: the stamp is written only once every rule held.
    assert _share_key_writes(recorder) == []
    assert await _last_used_at(world, world.keys["member"].id) is None
    # A key of a deleted context: refused, no use recorded either.
    await world.db.execute(update(Context).where(Context.id == world.d).values(deleted_at=utcnow()))
    await world.db.commit()
    await _refused_like_an_unknown_key(world, world.keys["member_y"].plaintext)
    assert await _last_used_at(world, world.keys["member_y"].id) is None
    # An honoured request records its use.
    assert (await _sessions(world, world.keys["owner"].plaintext)).status_code == 200
    assert await _last_used_at(world, world.keys["owner"].id) is not None


async def test_an_honoured_key_is_stamped_once_per_throttle_window(world) -> None:
    from datetime import timedelta

    from config.settings import get_settings
    from tests.integration._slot_world import StatementRecorder

    key = world.keys["owner"]
    with StatementRecorder(world.engine) as recorder:
        for _ in range(2):
            assert (await _sessions(world, key.plaintext)).status_code == 200
    # Two requests in one window: one write.
    assert len(_share_key_writes(recorder)) == 1
    stamped = await _last_used_at(world, key.id)
    assert stamped is not None

    # Past the window, the next honoured request stamps again.
    throttle = timedelta(seconds=get_settings().api_key_last_used_throttle_seconds)
    older = stamped - throttle - timedelta(seconds=1)
    await world.db.execute(update(ShareKey).where(ShareKey.id == key.id).values(last_used_at=older))
    await world.db.commit()
    with StatementRecorder(world.engine) as recorder:
        assert (await _sessions(world, key.plaintext)).status_code == 200
    assert len(_share_key_writes(recorder)) == 1
    assert await _last_used_at(world, key.id) > older


async def test_a_database_error_in_the_creator_check_is_a_server_error(world) -> None:
    from sqlalchemy.exc import OperationalError

    from services.permission_service import PermissionService

    failure = OperationalError("SELECT 1", {}, Exception("connection lost"))
    with patch.object(PermissionService, "check_context_rbac", AsyncMock(side_effect=failure)):
        response = await _sessions(world, world.keys["owner"].plaintext)
    assert response.status_code == 503
    assert response.json()["error"] == "DB-002"


async def test_a_programming_error_in_the_creator_check_is_a_server_error(world) -> None:
    from services.permission_service import PermissionService

    with patch.object(
        PermissionService, "check_context_rbac", AsyncMock(side_effect=RuntimeError("bug"))
    ):
        async with AsyncClient(
            transport=ASGITransport(app=world.app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/v1/share/sessions",
                headers={"Authorization": f"Bearer {world.keys['owner'].plaintext}"},
            )
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Housekeeping on departure
# ---------------------------------------------------------------------------


async def test_removing_a_member_revokes_its_share_keys_for_the_workspace(world) -> None:
    from services.workspace_service import WorkspaceService

    await world.db.commit()
    await WorkspaceService(world.db).remove_member(
        world.x.id, world.member, actor_user_id=world.owner
    )
    key, other = world.keys["member"], world.keys["member_y"]
    assert await _revoked_at(world, key.id) is not None
    assert await _revoked_at(world, other.id) is None
    (record,) = await _revocations(world, key.public_id)
    assert (record.actor_user_id, record.subject_user_id, record.context_id) == (
        world.owner,
        world.member,
        world.c,
    )
    assert await _revocations(world, other.public_id) == []

    # Idempotent: a second run revokes nothing and records nothing.
    again = await ShareKeyManager(world.db).revoke_creator_keys(
        world.member, workspace_id=world.x.id, actor_user_id=world.owner
    )
    await world.db.commit()
    assert again == 0
    assert len(await _revocations(world, key.public_id)) == 1

    # Re-added as a workspace admin (OWNER-level access to every shared
    # context): the key stays revoked.
    world.db.add(
        WorkspaceMember(
            workspace_id=world.x.id,
            user_id=world.member,
            role=WorkspaceRole.ADMIN,
        )
    )
    await world.db.commit()
    await _refused_like_an_unknown_key(world, key.plaintext)


async def test_deleting_an_account_deletes_its_share_keys(world) -> None:
    from api.routes.admin import delete_user

    await world.db.commit()
    with patch("api.routes.admin.logger") as logger:
        await delete_user(world.admin, {"user_id": "admin-runner"}, world.db)
    (line,) = [c for c in logger.info.call_args_list if c.args[0] == "admin_delete_user"]
    assert line.kwargs["share_keys_deleted"] == 1
    world.db.expire_all()
    left = await world.db.scalar(
        select(func.count()).select_from(ShareKey).where(ShareKey.user_id == world.admin)
    )
    assert left == 0
    assert await _revoked_at(world, world.keys["owner"].id) is None


async def test_erasing_an_account_deletes_its_share_keys(world, connector_world) -> None:
    from unittest.mock import MagicMock

    with (
        patch(
            "services.account_erasure_service.delete_user_points",
            new=AsyncMock(return_value={"kagura_memories": 0}),
        ),
        patch(
            "services.account_erasure_service.clear_co_activations",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "services.account_erasure_service.clear_user_rate_limits",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "services.account_erasure_service.get_redis_client",
            new=MagicMock(return_value=MagicMock(setex=AsyncMock(), delete=AsyncMock())),
        ),
        patch("services.account_erasure_service.get_session_manager", new=lambda: None),
        patch(
            "services.security_notification_service.purge_user_notification_state",
            AsyncMock(return_value=0),
        ),
    ):
        counts = await connector_world.erase(world.admin)
    assert counts["share_keys"] == 1
    left = await world.db.scalar(
        select(func.count()).select_from(ShareKey).where(ShareKey.user_id == world.admin)
    )
    assert left == 0
    assert await _revoked_at(world, world.keys["owner"].id) is None


# ---------------------------------------------------------------------------
# Owner oversight: revoke and list
# ---------------------------------------------------------------------------


async def _revoke(world, as_user: str, public_id: str):
    return await world.client.post(
        f"/api/v1/config/share-keys/{public_id}/revoke", headers={"x-test-session": as_user}
    )


@pytest.mark.parametrize("revoker", ["owner", "admin", "member"])
async def test_a_caller_who_can_share_the_context_revokes_any_key_bound_to_it(
    world, revoker
) -> None:
    # The admin's key, revoked by the workspace owner, the admin itself
    # (its creator) or the member who owns the context.
    target = world.keys["admin"] if revoker != "admin" else world.keys["owner"]
    actor = getattr(world, revoker)
    response = await _revoke(world, actor, target.public_id)
    assert response.status_code == 204
    await _refused_like_an_unknown_key(world, target.plaintext)
    (record,) = await _revocations(world, target.public_id)
    creator = world.admin if revoker != "admin" else world.owner
    assert (record.actor_user_id, record.subject_user_id) == (actor, creator)


async def test_anyone_else_gets_the_uniform_404(world) -> None:
    key = world.keys["owner"]
    answers = [
        await _revoke(world, world.plain, key.public_id),  # a member without the OWNER role
        await _revoke(world, world.stranger, key.public_id),  # another workspace
        await _revoke(world, world.owner, "skey_" + "0" * 22),  # unknown id
    ]
    assert await _revoked_at(world, key.id) is None
    assert (await _revoke(world, world.owner, key.public_id)).status_code == 204
    answers.append(await _revoke(world, world.owner, key.public_id))  # already revoked
    for response in answers:
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert answers[0].content == answers[-1].content


@pytest.mark.parametrize("caller", ["viewer", "plain", "stranger"])
async def test_a_caller_without_owner_level_access_gets_404_for_list_and_revoke(
    world, caller
) -> None:
    as_user = getattr(world, caller)
    listed = await world.client.get(
        "/api/v1/config/share-keys",
        params={"context_id": str(world.c)},
        headers={"x-test-session": as_user},
    )
    revoked = await _revoke(world, as_user, world.keys["owner"].public_id)
    assert (listed.status_code, listed.json()) == (404, CONTEXT_NOT_FOUND)
    assert (revoked.status_code, revoked.json()) == (404, NOT_FOUND)
    assert await _revoked_at(world, world.keys["owner"].id) is None


async def test_a_private_context_is_shared_by_its_creator_only(world) -> None:
    """The workspace owner does not reach a member's private context: its
    share keys are listed and revoked by their creator."""
    key = world.keys["private"]
    listed = await world.client.get(
        "/api/v1/config/share-keys",
        params={"context_id": str(world.p)},
        headers={"x-test-session": world.owner},
    )
    assert (listed.status_code, listed.json()) == (404, CONTEXT_NOT_FOUND)
    assert (await _revoke(world, world.owner, key.public_id)).status_code == 404
    assert (await _sessions(world, key.plaintext)).status_code == 200
    own = await world.client.get(
        "/api/v1/config/share-keys",
        params={"context_id": str(world.p)},
        headers={"x-test-session": world.member},
    )
    assert [k["id"] for k in own.json()] == [key.public_id]


async def test_the_context_list_of_a_suspended_workspace_is_refused_after_access(world) -> None:
    from models.workspace_slots import WorkspaceSlotSuspension
    from tests.integration._slot_world import T0, slot_mode

    world.db.add(
        WorkspaceSlotSuspension(
            workspace_id=world.x.id,
            owner_user_id=world.owner,
            cause="billing_snapshot",
            suspended_at=T0,
        )
    )
    await world.db.commit()
    try:
        with slot_mode("enforce"):
            listed = await world.client.get(
                "/api/v1/config/share-keys",
                params={"context_id": str(world.c)},
                headers={"x-test-session": world.owner},
            )
            own = await world.client.get(
                "/api/v1/config/share-keys", headers={"x-test-session": world.owner}
            )
            outsider = await world.client.get(
                "/api/v1/config/share-keys",
                params={"context_id": str(world.c)},
                headers={"x-test-session": world.plain},
            )
            revoked = await _revoke(world, world.owner, world.keys["admin"].public_id)
        assert listed.status_code == 403 and listed.json()["error"] == "SLOT-001"
        assert own.status_code == 200
        assert (outsider.status_code, outsider.json()) == (404, CONTEXT_NOT_FOUND)
        assert revoked.status_code == 204
    finally:
        await world.db.execute(
            delete(WorkspaceSlotSuspension).where(
                WorkspaceSlotSuspension.workspace_id == world.x.id
            )
        )
        await world.db.commit()


async def test_the_context_owner_lists_every_key_bound_to_the_context(world) -> None:
    for viewer in (world.owner, world.admin, world.member):
        response = await world.client.get(
            "/api/v1/config/share-keys",
            params={"context_id": str(world.c)},
            headers={"x-test-session": viewer},
        )
        assert response.status_code == 200
        listed = response.json()
        assert {k["id"] for k in listed} == {
            world.keys[label].public_id for label in ("member", "admin", "owner")
        }
        assert [k["created_at"] for k in listed] == sorted(
            (k["created_at"] for k in listed), reverse=True
        )
        assert {k["user_id"] for k in listed} == {world.member, world.admin, world.owner}


async def test_listing_another_context_is_the_context_routes_404(world) -> None:
    for viewer, ctx in ((world.plain, world.c), (world.stranger, world.c), (world.owner, uuid4())):
        response = await world.client.get(
            "/api/v1/config/share-keys",
            params={"context_id": str(ctx)},
            headers={"x-test-session": viewer},
        )
        assert (response.status_code, response.json()) == (404, CONTEXT_NOT_FOUND)


async def test_without_the_filter_the_list_is_the_callers_own_keys(world) -> None:
    response = await world.client.get(
        "/api/v1/config/share-keys", headers={"x-test-session": world.member}
    )
    assert response.status_code == 200
    assert {k["id"] for k in response.json()} == {
        world.keys["member"].public_id,
        world.keys["member_y"].public_id,
        world.keys["private"].public_id,
    }
