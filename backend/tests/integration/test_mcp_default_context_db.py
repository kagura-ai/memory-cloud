"""#2125: ``?context=<uuid>`` end to end against a real database.

The unit suite (``tests/mcp_server/test_dispatch_default_context.py``) pins
the dispatcher with the services stubbed. Here the real handlers, the real
``ContextService`` / ``PermissionService`` and real rows run:

* a ``remember`` without ``context_id`` through ``mcp_asgi_app`` with
  ``?context=`` on the URL lands in that context and a ``list`` without
  ``context_id`` reads it back (``list`` is the deterministic read; ``recall``
  needs the vector store, which this suite does not run);
* parity — a default naming a context the caller cannot use (another
  member's private context, a context in another workspace) answers with the
  same ``error`` and ``message`` as the explicit id, for a write and a read.

Only authentication (the transport's credential check) and the post-commit
embedding task are stubbed.
"""

import json
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, User, Workspace, WorkspaceMember


def _user(user_id: str, workspace_id) -> User:
    return User(
        email=f"{user_id}@example.test",
        user_id=user_id,
        name=user_id,
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
        current_workspace_id=workspace_id,
    )


def _workspace(name: str, owner_id: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=name,
        plan_name="pro",
        owner_user_id=owner_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def scenario(async_engine, db_session, monkeypatch):
    """Workspace A (owner + member) with a shared and a private context, and a
    workspace B the member does not belong to. The member's whitelist names
    both A contexts, so the private one is refused for being private (the
    member is not its creator), not for a whitelist miss."""
    import db.base as _db_base

    monkeypatch.setattr(
        _db_base,
        "async_session_factory",
        async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False),
    )

    tag = uuid4().hex[:8]
    owner_id, member_id, outsider_id = f"dc-owner-{tag}", f"dc-member-{tag}", f"dc-out-{tag}"
    ws = _workspace(f"ws-dc-{tag}", owner_id)
    other_ws = _workspace(f"ws-dc-other-{tag}", outsider_id)
    db_session.add_all([ws, other_ws])
    await db_session.flush()

    db_session.add_all(
        [_user(owner_id, ws.id), _user(member_id, ws.id), _user(outsider_id, other_ws.id)]
    )
    await db_session.flush()

    shared = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"dc-shared-{tag}",
        created_by=owner_id,
        is_private=False,
    )
    private = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"dc-private-{tag}",
        created_by=owner_id,
        is_private=True,
    )
    foreign = Context(
        id=uuid4(),
        workspace_id=other_ws.id,
        name=f"dc-foreign-{tag}",
        created_by=outsider_id,
        is_private=False,
    )
    db_session.add_all([shared, private, foreign])
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=owner_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=member_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[shared.id, private.id],
            ),
            WorkspaceMember(
                workspace_id=other_ws.id, user_id=outsider_id, role=WorkspaceRole.OWNER
            ),
        ]
    )
    await db_session.commit()

    yield {
        "ws_id": ws.id,
        "owner_id": owner_id,
        "member_id": member_id,
        "shared": shared,
        "private": private,
        "foreign": foreign,
    }

    from sqlalchemy import text

    ws_ids = [ws.id, other_ws.id]
    ctx_ids = [shared.id, private.id, foreign.id]
    user_ids = [owner_id, member_id, outsider_id]
    try:
        # Children first: revisions and access events reference the memories.
        for table in ("memory_revisions", "memory_access_events", "memories", "usage_stats"):
            await db_session.execute(
                text(f"DELETE FROM {table} WHERE context_id = ANY(:ids)"),  # noqa: S608 - fixed names
                {"ids": ctx_ids},
            )
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id.in_(ws_ids))
        )
        await db_session.execute(Context.__table__.delete().where(Context.workspace_id.in_(ws_ids)))
        await db_session.execute(User.__table__.delete().where(User.user_id.in_(user_ids)))
        await db_session.execute(Workspace.__table__.delete().where(Workspace.id.in_(ws_ids)))
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


@pytest.fixture(autouse=True)
def _clean_selection():
    from mcp_server.tools._helpers import (
        set_mcp_default_context_selection,
        set_mcp_key_workspace_scope,
    )

    set_mcp_default_context_selection(None)
    set_mcp_key_workspace_scope(None)
    yield
    set_mcp_default_context_selection(None)
    set_mcp_key_workspace_scope(None)


@pytest.fixture
def no_embedding():
    """The post-commit embedding task needs the model provider; skip it."""
    with patch("services.memory_service.MemoryService._schedule_embedding", lambda *_a: None):
        yield


# ------------------------------------------------------------- end to end


async def _asgi_call(monkeypatch, user_id: str, workspace_id, query: bytes, name: str, args: dict):
    import mcp_server.transport as transport
    from tests.mcp_server.test_transport_tool_profiles import (
        _modern_headers,
        _modern_request,
        _receive_for,
        _Recorder,
    )

    async def fake_auth(**_kwargs):
        return user_id, None, None

    async def fake_workspace(_user_id):
        return workspace_id

    monkeypatch.setattr(transport, "authenticate_mcp_request", fake_auth)
    monkeypatch.setattr(transport, "_get_user_workspace_id", fake_workspace)

    body = _modern_request("tools/call", name=name, arguments=args)
    send = _Recorder()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "query_string": query,
        "headers": list(_modern_headers(body).items()),
    }
    await transport.mcp_asgi_app(scope, _receive_for(body), send)
    assert send.status == 200, send.raw
    result = send.body["result"]
    return result, json.loads(result["content"][0]["text"])


@pytest.mark.asyncio(loop_scope="session")
async def test_remember_and_list_without_context_id_use_the_url_default(
    scenario, monkeypatch, no_embedding
):
    s = scenario
    query = f"profile=core&context={s['shared'].id}".encode()

    _, stored = await _asgi_call(
        monkeypatch,
        s["owner_id"],
        s["ws_id"],
        query,
        "remember",
        {"summary": "Default context end-to-end note", "content": "body", "type": "note"},
    )
    assert stored["status"] == "success", stored
    assert stored["context_id"] == str(s["shared"].id)
    assert stored["context_name"] == s["shared"].name

    _, listed = await _asgi_call(monkeypatch, s["owner_id"], s["ws_id"], query, "list", {})
    assert listed["status"] == "success", listed
    assert listed["context_id"] == str(s["shared"].id)
    assert stored["memory_id"] in json.dumps(listed)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_explicit_context_id_wins_over_the_url_on_real_rows(
    scenario, monkeypatch, no_embedding
):
    s = scenario
    # The URL names the private context; the explicit id names the shared one.
    query = f"context={s['private'].id}".encode()

    _, stored = await _asgi_call(
        monkeypatch,
        s["owner_id"],
        s["ws_id"],
        query,
        "remember",
        {
            "summary": "Explicit context beats the URL",
            "content": "body",
            "type": "note",
            "context_id": str(s["shared"].id),
        },
    )
    assert stored["status"] == "success", stored
    assert stored["context_id"] == str(s["shared"].id)


# ----------------------------------------------------------------- parity


async def _both(scenario, tool: str, args: dict, target) -> tuple[dict, dict]:
    from mcp_server.tools import execute_tool_call
    from mcp_server.tools._default_context import DefaultContextSelection
    from mcp_server.tools._helpers import set_mcp_default_context_selection

    user, ws = scenario["member_id"], scenario["ws_id"]
    set_mcp_default_context_selection(None)
    explicit = await execute_tool_call(tool, {**args, "context_id": str(target)}, user, ws)
    set_mcp_default_context_selection(DefaultContextSelection("url", target))
    implicit = await execute_tool_call(tool, args, user, ws)
    return json.loads(explicit[0].text), json.loads(implicit[0].text)


def _assert_parity(explicit: dict, implicit: dict) -> None:
    assert explicit["error"] == "context_not_found", explicit
    assert implicit["error"] == explicit["error"]
    assert implicit["message"] == explicit["message"]
    assert implicit["context_source"] == "url"
    assert "context_source" not in explicit
    rest = {k: v for k, v in implicit.items() if k not in ("context_source", "help")}
    assert rest == {k: v for k, v in explicit.items() if k != "help"}


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("target", ["private", "foreign"])
async def test_a_write_to_a_default_the_caller_cannot_use_matches_the_explicit_refusal(
    scenario, no_embedding, target
):
    args = {"summary": "Must never be stored here", "content": "body", "type": "note"}

    explicit, implicit = await _both(scenario, "remember", args, scenario[target].id)

    _assert_parity(explicit, implicit)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("target", ["private", "foreign"])
async def test_a_read_of_a_default_the_caller_cannot_use_matches_the_explicit_refusal(
    scenario, target
):
    explicit, implicit = await _both(scenario, "get_context_info", {}, scenario[target].id)

    _assert_parity(explicit, implicit)


@pytest.mark.asyncio(loop_scope="session")
async def test_the_member_can_use_a_shared_default(scenario):
    from mcp_server.tools import execute_tool_call
    from mcp_server.tools._default_context import DefaultContextSelection
    from mcp_server.tools._helpers import set_mcp_default_context_selection

    set_mcp_default_context_selection(DefaultContextSelection("url", scenario["shared"].id))
    result = await execute_tool_call(
        "get_context_info", {}, scenario["member_id"], scenario["ws_id"]
    )

    payload = json.loads(result[0].text)
    assert payload["status"] == "success", payload
    assert str(scenario["shared"].id) in json.dumps(payload)
