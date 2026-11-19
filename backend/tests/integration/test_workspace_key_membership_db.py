"""A workspace-scoped API key verifies only while its owner belongs to the workspace (#2928).

On real PostgreSQL rows:

* every key whose owner holds the workspace verifies as before: the owner
  (with and without a membership row), an admin, a member, a viewer, a member
  without a seat on the plan, an agent key of a member and of a viewer service
  identity, a connector key owned by an admin and one re-owned to the
  workspace owner;
* a global key and a public-bound key run no membership statement;
* a key scoped to a workspace its owner is not in (an owner who holds another
  workspace of their own), and a key of a soft-deleted workspace, do not
  verify: the REST dependency answers 401 with an unknown key's body, and MCP
  refuses at authentication, before the dispatcher counts the call against
  the daily MCP limit and before a served call would log a usage row;
* an agent key and a connector key whose owner's membership row is gone do
  not verify, and the refusal writes nothing (no agent ``last_seen_at``, no
  ``last_used_at``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from auth.api_keys import APIKeyManager
from models.agent import Agent
from models.auth import APIKey, Context, UsageStats, Workspace, WorkspaceMember
from services.plan_seats import member_seat_suspended
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

REJECTED_EVENT = "api_key_rejected_owner_not_in_workspace"

LEGITIMATE = [
    "owner",
    "owner_without_membership_row",
    "admin",
    "member",
    "viewer",
    "member_without_seat",
    "agent_of_member",
    "agent_of_viewer",
    "connector_of_admin",
    "connector_reowned_to_owner",
]
REFUSED = ["owner_not_in_workspace", "soft_deleted_workspace"]


@pytest_asyncio.fixture(loop_scope="session")
async def keys(connector_world, fernet_api_key_secret, async_engine):
    """One plaintext key per case, committed so other sessions see the rows."""
    w = connector_world
    owner = await w.user("vk-owner")
    admin, admin2, member, viewer = [
        await w.user(f"vk-{role}") for role in ("admin", "admin2", "member", "viewer")
    ]
    svc_member, svc_viewer = await w.user("vk-svc-member"), await w.user("vk-svc-viewer")
    svc_leaving, admin_leaving = await w.user("vk-svc-leaving"), await w.user("vk-admin-leaving")
    outsider, seatless = await w.user("vk-outsider"), await w.user("vk-seatless")
    owner2, gone_owner = await w.user("vk-owner2"), await w.user("vk-gone")

    x = await w.workspace(
        owner,
        **{
            admin: "admin",
            admin2: "admin",
            member: "member",
            viewer: "viewer",
            svc_member: "member",
            svc_viewer: "viewer",
            svc_leaving: "member",
            admin_leaving: "admin",
        },
    )
    # The outsider holds a workspace of their own (owner and membership row).
    await w.workspace(outsider)
    # The owner of this workspace has no membership row.
    no_row = Workspace(
        id=uuid4(),
        name=f"vk-norow-{uuid4().hex[:6]}",
        plan_name="promax",
        owner_user_id=owner2,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    w.db.add(no_row)
    w.workspace_ids.append(no_row.id)
    # A one-seat plan: the owner holds the seat, the member has none.
    one_seat = await w.workspace(owner, **{seatless: "member"})
    one_seat.plan_name = "free"
    gone = await w.workspace(gone_owner)
    gone.deleted_at = utcnow()
    agent = await w.agent(x, owner)
    public = Context(
        id=uuid4(),
        workspace_id=x.id,
        name=f"vk-public-{uuid4().hex[:6]}",
        created_by=member,
        is_public=True,
    )
    w.db.add(public)
    await w.db.flush()

    manager = APIKeyManager(w.db)

    async def mint(user_id: str, workspace: Workspace | None, **kwargs) -> str:
        plaintext, _ = await manager.create_key(
            name=f"vk-{uuid4().hex[:8]}",
            user_id=user_id,
            workspace_id=workspace.id if workspace is not None else None,
            **kwargs,
        )
        return plaintext

    plain = {
        "owner": await mint(owner, x),
        "owner_without_membership_row": await mint(owner2, no_row),
        "admin": await mint(admin, x),
        "member": await mint(member, x),
        "viewer": await mint(viewer, x),
        "member_without_seat": await mint(seatless, one_seat),
        "agent_of_member": await mint(svc_member, x, agent_id=agent.id),
        "agent_of_viewer": await mint(svc_viewer, x, agent_id=agent.id),
        "agent_of_leaving_member": await mint(svc_leaving, x, agent_id=agent.id),
        "global": await mint(outsider, None),
        "public_bound": await mint(member, None, bound_context_id=public.id),
        # Inserted directly: the owner of this key is not in X.
        "owner_not_in_workspace": await mint(outsider, x),
        "soft_deleted_workspace": await mint(gone_owner, gone),
    }
    by_admin = await w.connector(x, admin)
    plain["connector_of_admin"] = by_admin.plaintext_kmc_api_key
    by_admin2 = await w.connector(x, admin2)
    plain["connector_reowned_to_owner"] = by_admin2.plaintext_kmc_api_key
    reowned_connector_id = by_admin2.connector.id
    by_leaving_admin = await w.connector(x, admin_leaving)
    plain["connector_of_leaving_admin"] = by_leaving_admin.plaintext_kmc_api_key
    agent_id = agent.id
    x_id, no_row_id, one_seat_id, gone_id = x.id, no_row.id, one_seat.id, gone.id
    await w.db.commit()
    # Removing the admin re-owns the live connector keys to the workspace owner.
    await w.remove(x_id, admin2)
    reowned = await w.db.scalar(
        select(APIKey.user_id).where(APIKey.connector_id == reowned_connector_id)
    )
    assert reowned == owner
    one_seat = await w.db.get(Workspace, one_seat_id)
    seat_row = await w.db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == one_seat_id, WorkspaceMember.user_id == seatless
        )
    )
    assert one_seat is not None and seat_row is not None
    assert await member_seat_suspended(w.db, one_seat, seat_row)
    await w.db.commit()

    workspace_of = dict.fromkeys(plain, x_id)
    workspace_of.update(
        owner_without_membership_row=no_row_id,
        member_without_seat=one_seat_id,
        soft_deleted_workspace=gone_id,
        **{"global": None, "public_bound": None},
    )
    yield SimpleNamespace(
        db=w.db,
        engine=async_engine,
        factory=async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False),
        plain=plain,
        workspace_of=workspace_of,
        agent_id=agent_id,
        leaving={
            "agent_of_leaving_member": svc_leaving,
            "connector_of_leaving_admin": admin_leaving,
        },
    )


class _Statements:
    """The SQL text of every statement run on the engine while active."""

    def __init__(self, engine) -> None:
        self.engine = engine.sync_engine
        self.seen: list[str] = []

    def _record(self, _conn, _cursor, statement, *_args) -> None:
        self.seen.append(statement)

    def __enter__(self) -> list[str]:
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self.seen

    def __exit__(self, *_exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)


def _membership_statements(statements: list[str]) -> list[str]:
    return [s for s in statements if "workspace_members" in s]


@pytest.mark.parametrize("case", LEGITIMATE)
async def test_a_key_of_a_workspace_its_owner_holds_verifies(keys, case):
    verified = await APIKeyManager(keys.db).verify_key(keys.plain[case])

    assert verified is not None, case
    assert verified.workspace_id == keys.workspace_of[case]


async def test_a_workspace_key_runs_one_membership_statement(keys):
    with _Statements(keys.engine) as statements:
        assert await APIKeyManager(keys.db).verify_key(keys.plain["member"]) is not None

    assert len(_membership_statements(statements)) == 1, statements


@pytest.mark.parametrize("case", ["global", "public_bound"])
async def test_a_key_without_a_workspace_scope_runs_no_membership_statement(keys, case):
    with _Statements(keys.engine) as statements:
        verified = await APIKeyManager(keys.db).verify_key(keys.plain[case])

    assert verified is not None and verified.workspace_id is None
    assert [s for s in statements if "FROM api_keys" in s], "the recorder saw no key lookup"
    assert _membership_statements(statements) == []
    assert not [s for s in statements if "FROM workspaces" in s]


@pytest.mark.parametrize("case", REFUSED)
async def test_a_key_whose_owner_does_not_hold_the_workspace_does_not_verify(keys, case):
    with structlog.testing.capture_logs() as logs:
        verified = await APIKeyManager(keys.db).verify_key(keys.plain[case])

    assert verified is None
    events = [e for e in logs if e["event"] == REJECTED_EVENT]
    assert len(events) == 1, logs
    assert events[0]["workspace_id"] == str(keys.workspace_of[case])
    assert set(events[0]) >= {"key_prefix", "workspace_id"}
    assert "user_id" not in events[0]
    row = await keys.db.scalar(select(APIKey).where(APIKey.key_prefix == events[0]["key_prefix"]))
    assert row is not None and row.last_used_at is None


@pytest.mark.parametrize("case", ["agent_of_leaving_member", "connector_of_leaving_admin"])
async def test_a_refused_agent_or_connector_key_writes_nothing(keys, case):
    """The membership condition runs before the agent and connector gates: once
    the owner's membership row is gone the key does not verify, and neither the
    agent's ``last_seen_at`` nor the key's ``last_used_at`` is written."""
    db = keys.db
    plaintext = keys.plain[case]
    key = await db.scalar(select(APIKey).where(APIKey.key_prefix == plaintext[:16]))
    assert key is not None
    key_id, last_used = key.id, key.last_used_at
    seen_before = await db.scalar(select(Agent.last_seen_at).where(Agent.id == keys.agent_id))
    await db.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == keys.workspace_of[case],
            WorkspaceMember.user_id == keys.leaving[case],
        )
    )
    await db.flush()

    with structlog.testing.capture_logs() as logs:
        assert await APIKeyManager(db).verify_key(plaintext) is None

    assert [e["event"] for e in logs if e["event"].startswith("api_key_rejected")] == [
        REJECTED_EVENT
    ]
    db.expire_all()
    assert await db.scalar(select(APIKey.last_used_at).where(APIKey.id == key_id)) == last_used
    assert (
        await db.scalar(select(Agent.last_seen_at).where(Agent.id == keys.agent_id)) == seen_before
    )


def _rest_app(factory) -> FastAPI:
    from api.main import http_exception_handler, memory_cloud_exception_handler
    from api.routes import workspace
    from db.base import get_db
    from utils.exceptions import MemoryCloudException

    app = FastAPI()
    app.include_router(workspace.router, prefix="/api/v1")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]

    async def request_db():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = request_db
    return app


@pytest.mark.parametrize("case", [*REFUSED, "member"])
async def test_rest_authentication_answers_401_for_a_key_whose_owner_does_not_hold_the_workspace(
    keys, case
):
    async with AsyncClient(
        transport=ASGITransport(app=_rest_app(keys.factory)), base_url="https://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/workspace/embedding-status",
            headers={"Authorization": f"Bearer {keys.plain[case]}"},
        )

    expected = 200 if case == "member" else 401
    assert response.status_code == expected, response.text


@pytest.fixture
def mcp_state(keys, monkeypatch):
    """MCP on the test database, with the per-request scopes reset after."""
    import db.base as db_base
    from mcp_server.tools._helpers import (
        set_mcp_default_context_selection,
        set_mcp_key_workspace_scope,
    )

    monkeypatch.setattr(db_base, "async_session_factory", keys.factory)
    yield
    set_mcp_default_context_selection(None)
    set_mcp_key_workspace_scope(None)


async def _usage_rows(keys, workspace_id: UUID) -> int:
    async with keys.factory() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(UsageStats)
                .where(UsageStats.workspace_id == workspace_id)
            )
            or 0
        )


async def _mcp_call(keys, case: str, tool: str, arguments: dict):
    import mcp_server.transport as transport
    from tests.mcp_server.test_transport_tool_profiles import (
        _modern_headers,
        _modern_request,
        _receive_for,
        _Recorder,
    )

    body = _modern_request("tools/call", name=tool, arguments=arguments)
    headers = list(_modern_headers(body).items())
    headers.append((b"authorization", f"Bearer {keys.plain[case]}".encode()))
    send = _Recorder()
    await transport.mcp_asgi_app(
        {"type": "http", "method": "POST", "path": "/mcp/", "headers": headers},
        _receive_for(body),
        send,
    )
    return send


@pytest.mark.parametrize("case", [*REFUSED, "member"])
async def test_mcp_refuses_at_authentication_before_the_call_is_counted_or_logged(
    keys, mcp_state, case
):
    """Two probes: ``remember`` without a context is counted against the daily
    MCP limit and then stops; ``get_usage`` is served and logs a usage row. A
    member's key shows both effects, a refused key neither."""
    workspace_id = keys.workspace_of[case]
    rows_before = await _usage_rows(keys, workspace_id)
    rate_limit = AsyncMock(return_value=(True, 0, 1000))
    with patch("mcp_server.tools._check_rate_limit", new=rate_limit):
        counted = await _mcp_call(keys, case, "remember", {"summary": "x"})
        logged = await _mcp_call(keys, case, "get_usage", {})

    if case == "member":
        assert (counted.status, logged.status) == (200, 200), (counted.raw, logged.raw)
        # get_usage is exempt from the limit: only the remember call counted.
        rate_limit.assert_awaited_once_with(workspace_id)
        assert json.loads(logged.body["result"]["content"][0]["text"])["plan"] == "promax"
        assert await _usage_rows(keys, workspace_id) == rows_before + 1
        return
    assert (counted.status, logged.status) == (401, 401), (counted.raw, logged.raw)
    rate_limit.assert_not_awaited()
    assert await _usage_rows(keys, workspace_id) == rows_before


async def test_soft_deleted_workspace_key_reads_as_unknown(keys):
    """The deleted workspace's key and an unknown key get the same answer."""
    async with AsyncClient(
        transport=ASGITransport(app=_rest_app(keys.factory)), base_url="https://testserver"
    ) as client:
        deleted = await client.get(
            "/api/v1/workspace/embedding-status",
            headers={"Authorization": f"Bearer {keys.plain['soft_deleted_workspace']}"},
        )
        unknown = await client.get(
            "/api/v1/workspace/embedding-status",
            headers={"Authorization": "Bearer kagura_" + "x" * 43},
        )

    assert (deleted.status_code, deleted.json()) == (unknown.status_code, unknown.json())


async def test_a_membership_row_removed_later_stops_the_key(keys):
    """Membership is read on every verification, not when the key was minted."""
    member_key = keys.plain["viewer"]
    verified = await APIKeyManager(keys.db).verify_key(member_key)
    assert verified is not None
    await keys.db.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == verified.workspace_id,
            WorkspaceMember.user_id == verified.user_id,
        )
    )
    await keys.db.flush()

    assert await APIKeyManager(keys.db).verify_key(member_key) is None
