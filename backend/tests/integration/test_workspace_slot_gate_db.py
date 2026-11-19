"""The workspace slot gate on real rows (#2918): primitives, credentials, jobs.

One owner owns two workspaces: S (slot-suspended) and K (active). S has an
admin, a member whose context access list names only ``s1``, and nobody else;
an outsider belongs to neither.

* The permission primitives refuse every member of S with ``SLOT-001`` after
  authorization (the owner is pointed at the account page, a member is told to
  ask the owner), a member outside the reach list and an outsider keep the
  uniform 404, and ``OWNER_MANAGEMENT`` (export, delete, revoke) lets the owner
  through and nobody else.
* The credential matrix: session, OAuth, global key, workspace key, agent key
  and connector key (REST), resource token, share key, public endpoint, sync
  worker, observability principal; the internal sync control and the OIDC
  inspection keep working with a fixed answer.
* Mode ``off`` and ``shadow``: everything is served although the row exists,
  and no statement touches the slot tables (a statement recorder).
* The capacity purge and Sleep pause for S, and a fresh purge clock starts
  after the resume.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import WorkspaceRole
from models.agent import Agent
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import Memory
from models.workspace_slots import WorkspaceSlotSuspension
from services.capacity_lock import CapacityLock, CapacityPolicy
from services.permission_service import PermissionService, reachable_memory_filter
from services.workspace_slot_gate import SlotPolicy
from utils.exceptions import AuthorizationError, NotFoundException, WorkspaceSlotSuspendedError

pytestmark = pytest.mark.asyncio(loop_scope="session")

T0 = datetime(2031, 5, 1, 12, 0, 0)


def _mode(mode: str):
    return patch("services.workspace_slots.workspace_slot_mode", return_value=mode)


@dataclass
class World:
    session: AsyncSession
    factory: async_sessionmaker[AsyncSession]
    owner: str
    admin: str
    member: str
    outsider: str
    ws: dict[str, UUID] = field(default_factory=dict)
    ctx: dict[str, UUID] = field(default_factory=dict)
    mem: dict[str, UUID] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)


def _memory(ws: UUID, ctx: UUID, author: str) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=ws,
        context_id=ctx,
        user_id=author,
        summary="slot note",
        content="slot gate fixture",
        type="decision",
        scope="persistent",
        client="test",
        importance=0.5,
        embedding_status="success",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, admin, member, outsider = (f"slot-{r}-{tag}" for r in ("o", "a", "m", "x"))
    async with factory() as s:
        for uid in (owner, admin, member, outsider):
            s.add(User(email=f"{uid}@example.test", user_id=uid, name=uid, role="user"))
        await s.flush()
        ws: dict[str, UUID] = {}
        for key, plan, source in (
            ("s", "pro", "admin_grant"),
            ("k", "pro", "admin_grant"),
            # Free and billing-managed: a capacity-lock candidate (purge tests).
            ("f", "free", "external_billing"),
        ):
            row = Workspace(
                id=uuid4(),
                name=f"slot-{key}-{tag}",
                plan_name=plan,
                entitlement_source=source,
                owner_user_id=owner,
                daily_api_limit=50000,
                weekly_api_limit=250000,
            )
            s.add(row)
            ws[key] = row.id
        await s.flush()
        ctx: dict[str, UUID] = {}
        for key, wkey, private in (
            ("s1", "s", False),
            ("s2", "s", False),
            ("sp", "s", True),
            ("k1", "k", False),
            ("f1", "f", False),
        ):
            c = Context(
                id=uuid4(),
                workspace_id=ws[wkey],
                name=f"slot-{key}-{tag}",
                created_by=owner,
                is_private=private,
                sleep_mode="full",
            )
            s.add(c)
            ctx[key] = c.id
        await s.flush()
        s.add_all(
            [
                *(
                    WorkspaceMember(workspace_id=wid, user_id=owner, role=WorkspaceRole.OWNER)
                    for wid in ws.values()
                ),
                WorkspaceMember(workspace_id=ws["s"], user_id=admin, role=WorkspaceRole.ADMIN),
                WorkspaceMember(workspace_id=ws["k"], user_id=admin, role=WorkspaceRole.ADMIN),
                WorkspaceMember(
                    workspace_id=ws["s"],
                    user_id=member,
                    role=WorkspaceRole.MEMBER,
                    allowed_context_ids=[ctx["s1"]],
                ),
            ]
        )
        mems = {
            "s1": _memory(ws["s"], ctx["s1"], owner),
            "k1": _memory(ws["k"], ctx["k1"], owner),
        }
        s.add_all(list(mems.values()))
        agents = {}
        for key in ("s", "k"):
            agent = Agent(workspace_id=ws[key], name=f"slot-agent-{key}-{tag}", owner_user_id=admin)
            s.add(agent)
            await s.flush()
            agents[key] = agent
        from services.agent_binding_service import AgentBindingService

        for key, ckey in (("s", "s1"), ("k", "k1")):
            await AgentBindingService(s).create_binding(
                agent=agents[key], context_id=ctx[ckey], created_by=owner, can_read=True
            )
        for key in ("s", "f"):
            s.add(
                WorkspaceSlotSuspension(
                    workspace_id=ws[key],
                    owner_user_id=owner,
                    cause="billing_snapshot",
                    suspended_at=T0,
                )
            )
        await s.commit()
        w = World(
            session=s,
            factory=factory,
            owner=owner,
            admin=admin,
            member=member,
            outsider=outsider,
            ws=ws,
            ctx=ctx,
            mem={k: v.id for k, v in mems.items()},
            agents=agents,
        )
        try:
            yield w
        finally:
            set_agent_scope(None)
            await s.rollback()
            ids = list(ws.values())
            users = [owner, admin, member, outsider]
            from models.auth import ShareKey

            for stmt in (
                delete(WorkspaceSlotSuspension).where(
                    WorkspaceSlotSuspension.workspace_id.in_(ids)
                ),
                delete(ShareKey).where(ShareKey.user_id.in_(users)),
                delete(Agent).where(Agent.workspace_id.in_(ids)),
                delete(Workspace).where(Workspace.id.in_(ids)),
                delete(User).where(User.user_id.in_(users)),
            ):
                await s.execute(stmt)
                await s.commit()


async def _resume(w: World, key: str) -> None:
    await w.session.execute(
        delete(WorkspaceSlotSuspension).where(WorkspaceSlotSuspension.workspace_id == w.ws[key])
    )
    await w.session.commit()


# ---------------------------------------------------------------------------
# Permission primitives
# ---------------------------------------------------------------------------


async def test_every_member_of_a_suspended_workspace_is_refused(world: World) -> None:
    w, perm = world, PermissionService(world.session)
    with _mode("enforce"):
        with pytest.raises(WorkspaceSlotSuspendedError) as owner:
            await perm.check_workspace_access(w.owner, w.ws["s"], required_role="viewer")
        assert owner.value.details["workspace_id"] == str(w.ws["s"])
        assert str(owner.value.details["manage_url"]).endswith("/profile/workspaces")
        for uid in (w.admin, w.member):
            with pytest.raises(WorkspaceSlotSuspendedError) as other:
                await perm.check_workspace_access(uid, w.ws["s"], required_role="viewer")
            assert other.value.details["manage_url"] is None
        # A non-member keeps the uniform refusal: nothing new is learnt.
        with pytest.raises(AuthorizationError):
            await perm.check_workspace_access(w.outsider, w.ws["s"], required_role="viewer")
        # The active workspace serves everyone as before.
        await perm.check_workspace_access(w.owner, w.ws["k"], required_role="owner")
        await perm.check_workspace_access(w.admin, w.ws["k"], required_role="admin")


async def test_owner_management_lets_the_owner_through_and_nobody_else(world: World) -> None:
    w, perm = world, PermissionService(world.session)
    with _mode("enforce"):
        await perm.check_workspace_owner(w.owner, w.ws["s"], slot=SlotPolicy.OWNER_MANAGEMENT)
        with pytest.raises(WorkspaceSlotSuspendedError):
            await perm.check_workspace_access(
                w.admin, w.ws["s"], required_role="admin", slot=SlotPolicy.OWNER_MANAGEMENT
            )
        # Export: the owner exports, an admin is refused.
        from services.context_service import ContextService

        out = await ContextService(w.session).export_context(w.owner, w.ctx["s1"])
        assert out.memory_count == 1
        with pytest.raises(WorkspaceSlotSuspendedError):
            await ContextService(w.session).export_context(w.admin, w.ctx["s1"])


async def test_a_member_outside_the_reach_list_keeps_the_uniform_404(world: World) -> None:
    w, perm = world, PermissionService(world.session)
    with _mode("enforce"):
        for capacity in (CapacityPolicy.BLOCKED, CapacityPolicy.ALLOWED):
            with pytest.raises(NotFoundException):
                await perm.resolve_context_for_workspace_read(
                    w.member, w.ctx["s2"], capacity=capacity
                )
            with pytest.raises(NotFoundException):
                await perm.resolve_context_for_workspace_read(
                    w.outsider, w.ctx["s1"], capacity=capacity
                )
            with pytest.raises(NotFoundException):
                await perm.check_context_access(w.member, w.ctx["s2"], capacity=capacity)
            # Inside the list, the suspension answers — whatever the capacity policy.
            with pytest.raises(WorkspaceSlotSuspendedError):
                await perm.resolve_context_for_workspace_read(
                    w.member, w.ctx["s1"], capacity=capacity
                )
            with pytest.raises(WorkspaceSlotSuspendedError):
                await perm.check_context_access(w.admin, w.ctx["s1"], capacity=capacity)
            # The private-context creator path is gated too.
            with pytest.raises(WorkspaceSlotSuspendedError):
                await perm.check_context_access(w.owner, w.ctx["sp"], capacity=capacity)
        from services.context_service import ContextService

        with pytest.raises(NotFoundException):
            await ContextService(w.session).get_context(w.member, w.ctx["s2"])


async def test_context_service_and_memory_access(world: World) -> None:
    from services.context_service import ContextService

    w = world
    svc, perm = ContextService(w.session), PermissionService(w.session)
    with _mode("enforce"):
        with pytest.raises(WorkspaceSlotSuspendedError):
            await svc.get_context(w.owner, w.ctx["s1"])
        with pytest.raises(WorkspaceSlotSuspendedError):
            await svc.list_contexts(w.owner, workspace_id=w.ws["s"])
        with pytest.raises(WorkspaceSlotSuspendedError):
            await svc.create_context(w.ws["s"], f"new-{uuid4().hex[:6]}", created_by=w.owner)
        assert (await svc.get_context(w.owner, w.ctx["k1"])).id == w.ctx["k1"]
        # can_access_memory answers False (batch callers loop over it).
        args = {"user_id": w.owner, "memory_user_id": w.owner}
        assert not await perm.can_access_memory(
            **args, workspace_id=w.ws["s"], context_id=w.ctx["s1"]
        )
        assert await perm.can_access_memory(**args, workspace_id=w.ws["k"], context_id=w.ctx["k1"])
        # Cross-context reads leave the suspended workspace out.
        reach = await reachable_memory_filter(w.session, w.owner)
        ids = set((await w.session.execute(select(Memory.id).where(reach))).scalars())
        assert w.mem["k1"] in ids and w.mem["s1"] not in ids
    for mode in ("off", "shadow"):
        with _mode(mode):
            assert (await svc.get_context(w.owner, w.ctx["s1"])).id == w.ctx["s1"]
            assert await perm.can_access_memory(
                **args, workspace_id=w.ws["s"], context_id=w.ctx["s1"]
            )
            reach = await reachable_memory_filter(w.session, w.owner)
            ids = set((await w.session.execute(select(Memory.id).where(reach))).scalars())
            assert {w.mem["k1"], w.mem["s1"]} <= ids


@contextlib.contextmanager
def _statements(async_engine) -> Any:
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        seen.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_no_statement_touches_the_slot_tables_unless_enforced(
    world: World, async_engine, mode: str
) -> None:
    from mcp_server.tools._slot_gate import slot_suspension_refusal
    from services.context_service import ContextService

    w = world
    perm = PermissionService(w.session)
    with _mode(mode), _statements(async_engine) as seen:
        await perm.check_workspace_access(w.owner, w.ws["s"], required_role="owner")
        await perm.resolve_context_for_workspace_read(
            w.owner, w.ctx["s1"], capacity=CapacityPolicy.BLOCKED
        )
        await perm.check_context_access(w.owner, w.ctx["sp"], capacity=CapacityPolicy.BLOCKED)
        await ContextService(w.session).get_context(w.owner, w.ctx["s1"])
        await ContextService(w.session).list_contexts(w.owner, workspace_id=w.ws["s"])
        await perm.can_access_memory(
            user_id=w.owner,
            memory_user_id=w.owner,
            workspace_id=w.ws["s"],
            context_id=w.ctx["s1"],
        )
        await w.session.execute(
            select(Memory.id).where(await reachable_memory_filter(w.session, w.owner))
        )
        assert await slot_suspension_refusal("recall", {}, w.owner, w.ws["s"]) is None
    assert seen, "the recorder saw nothing"
    assert not [s for s in seen if "workspace_slot" in s]


# ---------------------------------------------------------------------------
# The credential matrix
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _rest(w: World, principal: dict) -> AsyncIterator[Any]:
    import httpx

    import api.routes.workspace as workspace_routes
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db as orig_get_db

    async def _db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as s:
            yield s

    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: principal
    for fn in {orig_get_db, workspace_routes.get_db}:
        app.dependency_overrides[fn] = _db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _principals(w: World, ws: UUID) -> dict[str, tuple[dict, AgentScope | None]]:
    base = {"user_id": w.admin, "current_workspace_id": ws, "role": "user"}
    key = "s" if ws == w.ws["s"] else "k"
    return {
        "session": ({**base, "auth_type": "session", "api_key_workspace_id": None}, None),
        "oauth": ({**base, "auth_type": "oauth", "api_key_workspace_id": None}, None),
        "global_key": ({**base, "auth_type": "api_key", "api_key_workspace_id": None}, None),
        "workspace_key": ({**base, "auth_type": "api_key", "api_key_workspace_id": ws}, None),
        "agent_key": (
            {**base, "auth_type": "api_key", "api_key_workspace_id": ws},
            AgentScope(agent_id=w.agents[key].id, enforcement_mode="enforce", workspace_id=ws),
        ),
        "connector_key": (
            {**base, "auth_type": "api_key", "api_key_workspace_id": ws, "connector_id": uuid4()},
            None,
        ),
    }


CREDENTIALS = ["session", "oauth", "global_key", "workspace_key", "agent_key", "connector_key"]


@pytest.mark.parametrize("credential", CREDENTIALS)
async def test_rest_credentials_are_refused_for_a_suspended_workspace(
    world: World, credential: str
) -> None:
    w = world
    for key, mode, expected in (
        ("s", "enforce", 403),
        ("k", "enforce", 200),
        ("s", "off", 200),
        ("s", "shadow", 200),
    ):
        principal, scope = _principals(w, w.ws[key])[credential]
        set_agent_scope(scope)
        try:
            with _mode(mode):
                async with _rest(w, principal) as client:
                    stats = await client.get("/api/v1/workspace/stats")
                    queue = await client.get("/api/v1/workspace/embedding-status")
        finally:
            set_agent_scope(None)
        assert (stats.status_code, queue.status_code) == (expected, expected), (
            key,
            mode,
            stats.text,
            queue.text,
        )
        if expected == 403:
            for resp in (stats, queue):
                body = resp.json()
                assert body["error"] == "SLOT-001", body
                assert body["details"]["gate"] == "workspace_slot"
                assert body["details"]["manage_url"] is None  # the admin is no owner


async def test_resource_tokens_are_refused_for_a_suspended_workspace(world: World) -> None:
    from api.routes.resource_ingest import _enforce_workspace_membership

    w = world
    request = SimpleNamespace(client=None)

    async def ingest(key: str) -> None:
        token = SimpleNamespace(
            created_by=w.owner,
            id=1,
            public_id="rtok_x",
            resource_pk=None,
            workspace_id=w.ws[key],
        )
        context = SimpleNamespace(workspace_id=w.ws[key], resource_id="r")
        await _enforce_workspace_membership(w.session, request, token, context)

    with _mode("enforce"):
        with pytest.raises(WorkspaceSlotSuspendedError) as exc:
            await ingest("s")
        # The token's holder learns nothing: no workspace id, no manage URL,
        # although the token's creator is the owner.
        assert exc.value.details == {"gate": "workspace_slot"}
        await ingest("k")
    with _mode("off"):
        await ingest("s")


async def test_share_keys_are_refused_for_a_suspended_workspace(world: World) -> None:
    from auth.dependencies import get_share_key_principal
    from auth.share_keys import ShareKeyManager

    w = world
    manager = ShareKeyManager(w.session)
    s_key, _ = await manager.create_key("s", w.owner, w.ctx["s1"])
    k_key, _ = await manager.create_key("k", w.owner, w.ctx["k1"])
    await w.session.commit()
    with _mode("enforce"):
        with pytest.raises(WorkspaceSlotSuspendedError) as exc:
            await get_share_key_principal(authorization=f"Bearer {s_key}", db=w.session)
        assert exc.value.details == {"gate": "workspace_slot"}  # the reader learns nothing
        principal = await get_share_key_principal(authorization=f"Bearer {k_key}", db=w.session)
        assert principal["share_key_context_id"] == w.ctx["k1"]
    with _mode("shadow"):
        await get_share_key_principal(authorization=f"Bearer {s_key}", db=w.session)


async def test_public_endpoints_pause_for_a_suspended_workspace(world: World) -> None:
    from mcp_server.public.publication import serving_paused

    w = world
    s_ws = await w.session.get(Workspace, w.ws["s"])
    k_ws = await w.session.get(Workspace, w.ws["k"])
    # A Pro plan carries public serving; only the suspension can pause it here.
    with _mode("enforce"):
        assert await serving_paused(w.session, SimpleNamespace(workspace=s_ws)) is True
        assert await serving_paused(w.session, SimpleNamespace(workspace=k_ws)) is False
    with _mode("off"):
        assert await serving_paused(w.session, SimpleNamespace(workspace=s_ws)) is False


async def test_the_sync_worker_is_refused_for_a_suspended_workspace(world: World) -> None:
    from services.sync_feed_authority import worker_target

    w = world
    w.session.info["paid_integration_write"] = True
    try:
        with (
            _mode("enforce"),
            patch("services.sync_feed_authority.exact_target", AsyncMock()),
        ):
            with pytest.raises(WorkspaceSlotSuspendedError) as exc:
                await worker_target(w.session, SimpleNamespace(workspace_id=w.ws["s"]))
            assert exc.value.details == {"gate": "workspace_slot"}
            # Reads (no content write) are refused as well.
            await worker_target(w.session, SimpleNamespace(workspace_id=w.ws["k"]))
    finally:
        w.session.info.pop("paid_integration_write", None)


async def test_observability_principals_are_refused_for_a_suspended_workspace(
    world: World,
) -> None:
    from auth import observation_credentials as obs

    w = world
    request = SimpleNamespace(headers={"authorization": "Bearer kagura_obs_x"})
    settings = SimpleNamespace(observability_api_enabled=True)

    def _verify(ws: UUID):
        return AsyncMock(return_value=SimpleNamespace(workspace_id=ws))

    for key, refused in (("s", True), ("k", False)):
        with (
            _mode("enforce"),
            patch.object(obs, "get_settings", return_value=settings),
            patch.object(obs, "_client_allowed", return_value=True),
            patch.object(obs, "_check_rate_limit", AsyncMock()),
            patch.object(obs.ObservationCredentialManager, "verify", _verify(w.ws[key])),
        ):
            if refused:
                with pytest.raises(WorkspaceSlotSuspendedError):
                    await obs.get_observation_principal(request, w.session)
            else:
                scope = await obs.get_observation_principal(request, w.session)
                assert scope.workspace_id == w.ws["k"]


async def test_internal_sync_control_keeps_working(world: World) -> None:
    """The payment service (no principal) never receives SLOT-001 (#2918 M11)."""
    from services.sync_authority import sync_authority

    w = world
    with _mode("enforce"):
        async with sync_authority(w.session, w.ws["s"], None) as authority:
            assert authority.workspace.id == w.ws["s"]
        await w.session.rollback()


async def test_the_oidc_inspection_reports_nothing_usable(world: World) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routes import me_workspaces
    from auth.oidc_bearer import identity_principal
    from db.base import get_db

    w = world
    app = FastAPI()
    app.include_router(me_workspaces.router, prefix="/api/v1")

    async def _db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as s:
            yield s

    app.dependency_overrides[identity_principal] = lambda: SimpleNamespace(user_id=w.owner)
    app.dependency_overrides[get_db] = _db
    url = f"/api/v1/me/workspaces/{w.ws['s']}/access?context_id={w.ctx['s1']}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        with _mode("enforce"):
            suspended = await client.get(url)
        with _mode("off"):
            served = await client.get(url)
    assert suspended.status_code == 200, suspended.text
    body = suspended.json()
    assert body["resource"]["permissions"] == {"read": False, "sync.manage": False}
    assert body["workspace_permissions"] == {"billing.manage": True, "usage.read": False}
    assert served.status_code == 200, served.text
    assert served.json()["resource"]["permissions"].get("read") is True


# ---------------------------------------------------------------------------
# Jobs: the capacity purge and Sleep pause
# ---------------------------------------------------------------------------


def _lock() -> CapacityLock:
    return CapacityLock(
        memory_count=2,
        memory_limit=1,
        over_memories=1,
        used_bytes=0,
        storage_limit_bytes=1,
        over_bytes=0,
        cleanup_url="https://app.example.com/workspace/settings/plan",
    )


async def test_the_purge_pauses_and_restarts_after_the_resume(world: World) -> None:
    from services import capacity_purge

    w = world
    ws = await w.session.get(Workspace, w.ws["f"])
    assert ws is not None
    with (
        _mode("enforce"),
        patch.object(capacity_purge, "capacity_lock_state", AsyncMock(return_value=_lock())),
    ):
        # Suspended: never stamped.
        assert await capacity_purge._stamp_one(w.session, ws, now=T0) is None
        assert ws.capacity_locked_since is None
        # An existing stamp is cleared, notices with it.
        ws.capacity_locked_since = T0
        ws.capacity_purge_first_notice_at = T0
        assert await capacity_purge._stamp_one(w.session, ws, now=T0) == "cleared"
        assert (ws.capacity_locked_since, ws.capacity_purge_first_notice_at) == (None, None)
        await w.session.commit()  # the stamp sweep commits its batch
        assert await capacity_purge._workspace_state(w.session, ws) == "left"
        # Resumed: a fresh clock starts on the next run.
        await _resume(w, "f")
        later = datetime(2031, 6, 1, 12, 0, 0)
        assert await capacity_purge._stamp_one(w.session, ws, now=later) == "stamped"
        assert ws.capacity_locked_since == later
        assert await capacity_purge._workspace_state(w.session, ws) == "purgeable"
        ws.capacity_locked_since = None
        await w.session.commit()


async def test_sleep_skips_a_suspended_workspace(world: World) -> None:
    from services.sleep.orchestrator import SleepOrchestrator

    w = world
    orchestrator = SleepOrchestrator(w.session)
    with _mode("enforce"):
        assert await orchestrator._get_sleep_mode(str(w.ctx["s1"])) == "skip"
        assert await orchestrator._get_sleep_mode(str(w.ctx["k1"])) == "full"
    with _mode("shadow"):
        assert await orchestrator._get_sleep_mode(str(w.ctx["s1"])) == "full"


# ---------------------------------------------------------------------------
# Owner management over REST: revoking a credential
# ---------------------------------------------------------------------------


async def test_revoking_a_credential_is_never_blocked(world: World) -> None:
    """REVOCATION: the route's own authorization decides, as for an active
    workspace — the owner, an admin, and a member for their own key."""
    import httpx

    from api.main import app
    from auth.dependencies import require_session_auth
    from db.base import get_db
    from models.auth import APIKey

    w = world

    async def _key() -> str:
        key = APIKey(
            user_id=w.member,
            name=f"slot-{uuid4().hex[:6]}",
            key_hash=uuid4().hex + uuid4().hex,
            key_prefix="kmc_" + uuid4().hex[:8],
            workspace_id=w.ws["s"],
        )
        w.session.add(key)
        await w.session.commit()
        return key.public_id

    async def _db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as s:
            yield s
            await s.commit()

    base = f"/api/v1/workspaces/{w.ws['s']}/members/{w.member}/credentials"
    app.dependency_overrides[get_db] = _db
    results = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            with _mode("enforce"):
                for caller in (w.admin, w.owner):
                    await _key()
                    app.dependency_overrides[require_session_auth] = lambda c=caller: {"user_id": c}
                    results.append(await client.delete(f"{base}/api-key"))
                # A member revoking their own key (the session self-path).
                own = await _key()
                principal = {"user_id": w.member, "sub": w.member, "auth_type": "session"}
                from auth.dependencies import get_user_from_api_key_or_session

                app.dependency_overrides[get_user_from_api_key_or_session] = lambda: principal
                results.append(await client.delete(f"{base}/api-keys/{own}"))
                # Minting stays blocked.
                app.dependency_overrides[require_session_auth] = lambda: {"user_id": w.member}
                minted = await client.post(f"{base}/api-key/regenerate")
    finally:
        app.dependency_overrides.clear()
    for resp in results:
        assert resp.status_code == 200, resp.text
    assert minted.status_code == 403, minted.text
    assert minted.json()["error"] == "SLOT-001"


# ---------------------------------------------------------------------------
# Review round: membership, health, jobs, notices, keys, admin, consent
# ---------------------------------------------------------------------------


async def test_an_invitation_to_a_suspended_workspace_is_refused(world: World) -> None:
    from models.auth import WorkspaceInvitation
    from services.invitation_service import InvitationService

    w = world
    token = uuid4().hex + uuid4().hex
    w.session.add(
        WorkspaceInvitation(
            workspace_id=w.ws["s"], token=token, role=WorkspaceRole.MEMBER, invited_by=w.owner
        )
    )
    await w.session.commit()
    with _mode("enforce"), pytest.raises(WorkspaceSlotSuspendedError) as exc:
        await InvitationService(w.session).accept_invitation(
            token, w.outsider, f"{w.outsider}@example.test"
        )
    assert exc.value.details == {"gate": "workspace_slot"}
    await w.session.rollback()
    joined = await w.session.scalar(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == w.ws["s"], WorkspaceMember.user_id == w.outsider
        )
    )
    assert joined is None
    current = await w.session.scalar(
        select(User.current_workspace_id).where(User.user_id == w.outsider)
    )
    assert current != w.ws["s"]


async def test_memory_health_leaves_a_suspended_workspace_out(world: World) -> None:
    from services.memory_health_service import MemoryHealthService

    w = world
    with _mode("enforce"):
        scope = await MemoryHealthService(w.session)._resolve_caller_scope(w.owner)
    assert w.ws["s"] not in scope.open_workspaces and w.ws["k"] in scope.open_workspaces
    with _mode("off"):
        scope = await MemoryHealthService(w.session)._resolve_caller_scope(w.owner)
    assert {w.ws["s"], w.ws["k"]} <= scope.open_workspaces


async def test_the_nightly_sleep_run_skips_a_suspended_workspace(world: World) -> None:
    from tasks.sleep_tasks import sleep_targets_statement

    w = world
    with _mode("enforce"):
        rows = (await w.session.execute(sleep_targets_statement())).all()
    contexts = {row[2] for row in rows}
    assert w.ctx["k1"] in contexts and w.ctx["s1"] not in contexts
    with _mode("shadow"):
        rows = (await w.session.execute(sleep_targets_statement())).all()
    assert {w.ctx["k1"], w.ctx["s1"]} <= {row[2] for row in rows}


async def test_no_countdown_mail_for_a_suspended_workspace(world: World) -> None:
    from services import capacity_purge

    w = world
    ws = await w.session.get(Workspace, w.ws["f"])
    assert ws is not None
    ws.capacity_locked_since = datetime(2031, 4, 1, 12, 0, 0)  # 30 days before T0
    await w.session.commit()
    email = AsyncMock()
    try:
        with (
            _mode("enforce"),
            patch.object(capacity_purge, "capacity_lock_state", AsyncMock(return_value=_lock())),
        ):
            counts = await capacity_purge.send_due_purge_notices(
                w.session, now=T0, redis=AsyncMock(), email=email
            )
        assert counts.sent == 0 and counts.skipped >= 1
        await w.session.refresh(ws)
        assert (ws.capacity_purge_first_notice_at, ws.capacity_purge_final_notice_at) == (
            None,
            None,
        )
        assert not email.mock_calls
    finally:
        ws.capacity_locked_since = None
        await w.session.commit()


async def test_a_running_purge_stops_with_slot_suspended(world: World) -> None:
    from models.auth import AuditLog
    from services import capacity_purge
    from services.context_service import ContextService, WorkspacePurgeReport

    w = world
    await _resume(w, "f")
    ws = await w.session.get(Workspace, w.ws["f"])
    assert ws is not None
    ws.capacity_locked_since = datetime(2031, 3, 1, 12, 0, 0)
    ws.capacity_purge_first_notice_at = datetime(2031, 3, 20, 12, 0, 0)
    ws.capacity_purge_final_notice_at = datetime(2031, 3, 30, 12, 0, 0)
    await w.session.commit()

    async def purge(self, ws_id, *, actor, should_continue):  # noqa: ARG001
        # The owner's allowance dropped while the purge ran.
        w.session.add(
            WorkspaceSlotSuspension(
                workspace_id=ws_id, owner_user_id=w.owner, cause="admin_plan", suspended_at=T0
            )
        )
        await w.session.flush()
        assert await should_continue() is False
        return WorkspacePurgeReport(0, 0, 0, 0, 0, aborted=True)

    with (
        _mode("enforce"),
        patch.object(capacity_purge, "capacity_lock_state", AsyncMock(return_value=_lock())),
        patch.object(capacity_purge, "_owner_contact", AsyncMock(return_value=None)),
        patch.object(ContextService, "purge_workspace_data", purge),
    ):
        counts = await capacity_purge.purge_expired_workspaces(w.session, now=T0, email=AsyncMock())
    assert counts.aborted == 1, counts
    reasons = [
        (row.user_metadata or {}).get("aborted_reason")
        for row in (
            await w.session.execute(
                select(AuditLog).where(AuditLog.resource == f"workspace:{w.ws['f']}")
            )
        ).scalars()
    ]
    assert "slot_suspended" in reasons, reasons


@contextlib.asynccontextmanager
async def _client(w: World, overrides: dict) -> AsyncIterator[Any]:
    import httpx

    from api.main import app
    from db.base import get_db

    async def _db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides.update(overrides)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def test_a_global_key_of_a_suspended_workspace_is_not_regenerated(world: World) -> None:
    from auth.dependencies import require_session_auth
    from models.auth import APIKey

    w = world
    key = APIKey(
        user_id=w.owner,
        name=f"slot-{uuid4().hex[:6]}",
        key_hash=uuid4().hex + uuid4().hex,
        key_prefix="kmc_" + uuid4().hex[:8],
        workspace_id=w.ws["s"],
    )
    w.session.add(key)
    await w.session.commit()
    session = {require_session_auth: lambda: {"user_id": w.owner, "sub": w.owner}}
    async with _client(w, session) as client:
        with _mode("enforce"):
            regenerated = await client.post(f"/api/v1/config/api-keys/{key.public_id}/regenerate")
            deleted = await client.delete(f"/api/v1/config/api-keys/{key.public_id}")
    assert regenerated.status_code == 403, regenerated.text
    assert regenerated.json()["error"] == "SLOT-001"
    assert deleted.status_code in (200, 204), deleted.text


async def test_the_admin_user_page_loads_with_a_suspended_workspace(world: World) -> None:
    from auth.dependencies import require_admin

    w = world
    admin = {require_admin: lambda: {"user_id": "operator", "role": "admin"}}
    async with _client(w, admin) as client:
        with _mode("enforce"):
            resp = await client.get(f"/api/v1/admin/users/{w.owner}")
    assert resp.status_code == 200, resp.text


async def test_the_consent_page_keeps_the_other_workspaces_contexts(world: World) -> None:
    from services.credential_default_context import readable_context_options

    w = world
    with _mode("enforce"):
        options = await readable_context_options(w.session, user_id=w.owner)
    ids = {o.context_id for o in options}
    assert w.ctx["k1"] in ids and w.ctx["s1"] not in ids


# ---------------------------------------------------------------------------
# Owner management over REST
# ---------------------------------------------------------------------------


async def test_export_works_for_the_owner_only(world: World) -> None:
    from auth.dependencies import get_user_from_api_key_or_session

    w = world
    url = f"/api/v1/contexts/{w.ctx['s1']}/export"
    principals = {
        "owner session": ({"user_id": w.owner, "auth_type": "session"}, 200),
        "owner key": (
            {"user_id": w.owner, "auth_type": "api_key", "api_key_workspace_id": w.ws["s"]},
            200,
        ),
        "admin session": ({"user_id": w.admin, "auth_type": "session"}, 403),
    }
    for label, (principal, expected) in principals.items():
        overrides = {get_user_from_api_key_or_session: lambda p=principal: p}
        async with _client(w, overrides) as client:
            with _mode("enforce"):
                resp = await client.get(url)
        assert resp.status_code == expected, (label, resp.text)
        if expected == 403:
            assert resp.json()["error"] == "SLOT-001", label


async def test_switching_away_works_and_switching_to_a_suspended_workspace_is_refused(
    world: World,
) -> None:
    w = world
    with (
        _mode("enforce"),
        patch(
            "api.routes.workspaces.get_current_user",
            AsyncMock(return_value={"user_id": w.owner}),
        ),
    ):
        async with _client(w, {}) as client:
            away = await client.put(f"/api/v1/workspaces/{w.ws['k']}/switch")
            into = await client.put(f"/api/v1/workspaces/{w.ws['s']}/switch")
    assert away.status_code == 200, away.text
    assert into.status_code == 403, into.text
    assert into.json()["error"] == "SLOT-001"


async def test_billing_handoff_passes_the_gate_for_the_owner_only(world: World) -> None:
    from auth.dependencies import require_session_auth

    w = world
    body = {"workspace_id": str(w.ws["s"])}
    outcomes = {}
    for caller in (w.owner, w.admin):
        overrides = {require_session_auth: lambda c=caller: {"user_id": c, "sub": c}}
        async with _client(w, overrides) as client:
            with _mode("enforce"):
                outcomes[caller] = await client.post("/api/v1/billing/handoff", json=body)
    # The owner is never refused by the gate (the handoff itself may need
    # billing settings this test does not configure).
    owner = outcomes[w.owner]
    assert owner.status_code != 403 or owner.json().get("error") != "SLOT-001", owner.text
    assert outcomes[w.admin].status_code == 403, outcomes[w.admin].text


async def test_the_owner_deletes_a_suspended_workspace_and_an_admin_cannot(world: World) -> None:
    w = world
    for caller, expected in ((w.admin, 403), (w.owner, 204)):
        with (
            _mode("enforce"),
            patch(
                "api.routes.workspaces.get_current_user",
                AsyncMock(return_value={"user_id": caller}),
            ),
        ):
            async with _client(w, {}) as client:
                resp = await client.delete(f"/api/v1/workspaces/{w.ws['s']}")
        assert resp.status_code == expected, (caller, resp.text)
    w.session.expire_all()
    deleted = await w.session.scalar(select(Workspace.deleted_at).where(Workspace.id == w.ws["s"]))
    assert deleted is not None


# ---------------------------------------------------------------------------
# The write resolver over REST: confinement before the gate
# ---------------------------------------------------------------------------


async def test_rest_remember_confines_a_key_and_a_binding_before_the_gate(world: World) -> None:
    """A key scoped to the active K naming a context of the suspended S, and an
    enforce-mode agent key whose binding does not name the context, get the
    uniform 404 rather than S's suspension."""
    w = world
    body = {
        "summary": "a slot gate note",
        "content": "a slot gate note body",
        "type": "decision",
        "context": {"context_id": str(w.ctx["s2"])},
    }
    cases = {
        "key scoped to K": (
            {
                "user_id": w.owner,
                "auth_type": "api_key",
                "api_key_workspace_id": w.ws["k"],
                "current_workspace_id": w.ws["k"],
                "role": "user",
            },
            None,
        ),
        "agent key of S": (
            {
                "user_id": w.admin,
                "auth_type": "api_key",
                "api_key_workspace_id": w.ws["s"],
                "current_workspace_id": w.ws["s"],
                "role": "user",
            },
            AgentScope(
                agent_id=w.agents["s"].id, enforcement_mode="enforce", workspace_id=w.ws["s"]
            ),
        ),
    }
    for label, (principal, scope) in cases.items():
        set_agent_scope(scope)
        try:
            with _mode("enforce"):
                async with _rest(w, principal) as client:
                    resp = await client.post("/api/v1/memory/remember", json=body)
        finally:
            set_agent_scope(None)
        assert resp.status_code == 404, (label, resp.text)
        assert resp.json()["error"] != "SLOT-001", label


# ---------------------------------------------------------------------------
# Account routes that name a default context in a suspended workspace
# ---------------------------------------------------------------------------


async def test_a_default_context_in_a_suspended_workspace_reads_as_no_name(world: World) -> None:
    """Listing keys and OAuth grants and regenerating a global key keep working
    when the default context sits in a suspended workspace; setting such a
    default answers the uniform not-found."""
    from auth.api_keys import APIKeyManager
    from auth.dependencies import require_session_auth
    from models.auth import APIKey, OAuth2Client, OAuthGrantSetting

    w = world
    for caller in (w.owner, w.member):
        client_id = f"c-{uuid4().hex[:12]}"
        w.session.add(
            OAuth2Client(
                client_id=client_id,
                client_secret_hash="x" * 64,
                client_name="Slot client",
                redirect_uris=["http://127.0.0.1/cb"],
                grant_types=["authorization_code"],
                response_types=["code"],
                scope="memory:read",
                token_endpoint_auth_method="none",
            )
        )
        await w.session.flush()
        w.session.add(
            OAuthGrantSetting(user_id=caller, client_id=client_id, default_context_id=w.ctx["s1"])
        )
        _, key = await APIKeyManager(w.session).create_key(
            name=f"slot-{uuid4().hex[:6]}", user_id=caller, default_context_id=w.ctx["s1"]
        )
        await w.session.commit()
        session = {require_session_auth: lambda c=caller: {"user_id": c, "sub": c}}
        try:
            async with _client(w, session) as client:
                with _mode("enforce"):
                    keys = await client.get("/api/v1/config/api-keys")
                    grants = await client.get("/api/v1/me/oauth/grants")
                    regenerated = await client.post(
                        f"/api/v1/config/api-keys/{key.public_id}/regenerate"
                    )
                    created = await client.post(
                        "/api/v1/config/api-keys",
                        json={
                            "name": f"slot-{uuid4().hex[:6]}",
                            "default_context_id": str(w.ctx["s1"]),
                        },
                    )
                    put_default = await client.put(
                        f"/api/v1/me/oauth/grants/{client_id}/default-context",
                        json={"default_context_id": str(w.ctx["s1"])},
                    )
        finally:
            await w.session.rollback()
            await w.session.execute(
                delete(OAuthGrantSetting).where(OAuthGrantSetting.client_id == client_id)
            )
            await w.session.execute(delete(OAuth2Client).where(OAuth2Client.client_id == client_id))
            await w.session.execute(delete(APIKey).where(APIKey.user_id == caller))
            await w.session.commit()
        assert keys.status_code == 200, (caller, keys.text)
        listed = {k["id"]: k for k in keys.json()}
        assert listed[key.public_id]["default_context_name"] is None, caller
        assert grants.status_code == 200, (caller, grants.text)
        (grant,) = [g for g in grants.json() if g["client_id"] == client_id]
        assert grant["default_context_name"] is None, caller
        assert regenerated.status_code in (200, 201), (caller, regenerated.text)
        assert regenerated.json()["api_key"], caller
        assert regenerated.json()["default_context_name"] is None, caller
        assert created.status_code == 404, (caller, created.text)
        assert put_default.status_code == 404, (caller, put_default.text)


async def test_a_contracted_sibling_keeps_the_deletion_guard(world: World) -> None:
    """Enforce: a workspace under a billing contract next to a suspended one
    still answers 409 BILLING-005 to its owner's delete (a suspended workspace
    is never under a contract and deletes)."""
    from sqlalchemy import update

    w = world
    await w.session.execute(
        update(Workspace)
        .where(Workspace.id == w.ws["k"])
        .values(plan_name="pro", entitlement_source="external_billing")
    )
    await w.session.commit()
    with (
        _mode("enforce"),
        patch(
            "api.routes.workspaces.get_current_user",
            AsyncMock(return_value={"user_id": w.owner}),
        ),
    ):
        async with _client(w, {}) as client:
            resp = await client.delete(f"/api/v1/workspaces/{w.ws['k']}")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "BILLING-005"
    w.session.expire_all()
    assert (
        await w.session.scalar(select(Workspace.deleted_at).where(Workspace.id == w.ws["k"]))
        is None
    )
