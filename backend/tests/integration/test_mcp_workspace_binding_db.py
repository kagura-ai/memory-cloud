"""#3000 / #3001 / #3002: an MCP connection's workspace, against a real database.

* #3000: the connection's default context (``?context=`` or the credential's
  own) binds the connection to that context's workspace when the caller is a
  member there and reaches the context; the URL's workspace and a
  workspace-scoped key keep precedence; anything else falls back to the
  current workspace, which is never written.
* #3001: ``list_contexts`` of an unbound connection lists every member
  workspace, labelled, paged with one keyset cursor; a bound one is unchanged.
* #3002: ``create_context`` names its workspace, takes ``workspace_id``, and
  an unbound caller who may create in several workspaces must pick one.

The real handlers, ``ContextService`` and ``PermissionService`` run; only the
transport's credential check and the post-commit embedding task are stubbed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, UsageStats, User, Workspace, WorkspaceMember


def _workspace(tag: str, name: str, owner_id: str, plan_name: str = "pro") -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"{name}-{tag}",
        plan_name=plan_name,
        owner_user_id=owner_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _user(user_id: str, current_workspace_id) -> User:
    return User(
        email=f"{user_id}@example.test",
        user_id=user_id,
        name=user_id,
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
        current_workspace_id=current_workspace_id,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine, db_session, monkeypatch):
    """``user`` (current workspace A) in five workspaces of different kinds.

    - A: owner. ``a_shared``, ``a_private`` (the user's), ``a_dup`` (same name
      as ``b_dup``), ``a_deleted`` (soft-deleted)
    - B: admin. ``b_shared``, ``b_dup``, ``b_private_other`` (another member's
      private context)
    - C: member with ``c_allowed`` on the access list, not ``c_hidden``
    - D: a free (one-seat) workspace of ``other`` where the user has no seat
    - E: not a member (``e1``)
    - ``member``: a member of A only (no workspace to create in)
    """
    import db.base as _db_base

    monkeypatch.setattr(
        _db_base,
        "async_session_factory",
        async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False),
    )

    tag = uuid4().hex[:8]
    user_id, other_id, outsider_id, member_id = (
        f"wb-user-{tag}",
        f"wb-other-{tag}",
        f"wb-out-{tag}",
        f"wb-member-{tag}",
    )
    wa = _workspace(tag, "wb-a", user_id)
    wb = _workspace(tag, "wb-b", other_id)
    wc = _workspace(tag, "wb-c", other_id)
    wd = _workspace(tag, "wb-d", other_id, plan_name="free")
    we = _workspace(tag, "wb-e", outsider_id)
    workspaces = [wa, wb, wc, wd, we]
    db_session.add_all(workspaces)
    await db_session.flush()
    db_session.add_all(
        [
            _user(user_id, wa.id),
            _user(other_id, wb.id),
            _user(outsider_id, we.id),
            _user(member_id, wa.id),
        ]
    )
    await db_session.flush()

    now = datetime.now(UTC)

    def _ctx(workspace: Workspace, name: str, created_by: str, age: int | None, **kwargs):
        return Context(
            id=uuid4(),
            workspace_id=workspace.id,
            name=f"{name}-{tag}",
            created_by=created_by,
            last_used_at=None if age is None else now - timedelta(minutes=age),
            **kwargs,
        )

    a_shared = _ctx(wa, "a-shared", user_id, 5, is_private=False)
    a_private = _ctx(wa, "a-private", user_id, 30, is_private=True)
    a_dup = _ctx(wa, "dup", user_id, 50, is_private=False)
    a_deleted = _ctx(
        wa, "a-deleted", user_id, 1, is_private=False, deleted_at=now.replace(tzinfo=None)
    )
    b_shared = _ctx(wb, "b-shared", other_id, 1, is_private=False)
    b_dup = _ctx(wb, "dup", other_id, 40, is_private=False)
    b_private_other = _ctx(wb, "b-private-other", other_id, 2, is_private=True)
    c_allowed = _ctx(wc, "c-allowed", other_id, 120, is_private=False)
    c_hidden = _ctx(wc, "c-hidden", other_id, 3, is_private=False)
    d1 = _ctx(wd, "d1", other_id, 4, is_private=False)
    e1 = _ctx(we, "e1", outsider_id, 6, is_private=False)
    contexts = [
        a_shared,
        a_private,
        a_dup,
        a_deleted,
        b_shared,
        b_dup,
        b_private_other,
        c_allowed,
        c_hidden,
        d1,
        e1,
    ]
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=wa.id, user_id=user_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(
                workspace_id=wa.id,
                user_id=member_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[a_shared.id],
            ),
            WorkspaceMember(workspace_id=wb.id, user_id=other_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=wb.id, user_id=user_id, role=WorkspaceRole.ADMIN),
            WorkspaceMember(workspace_id=wc.id, user_id=other_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(
                workspace_id=wc.id,
                user_id=user_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[c_allowed.id],
            ),
            WorkspaceMember(workspace_id=wd.id, user_id=other_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=wd.id, user_id=user_id, role=WorkspaceRole.MEMBER),
            WorkspaceMember(workspace_id=we.id, user_id=outsider_id, role=WorkspaceRole.OWNER),
            *contexts,
        ]
    )
    await db_session.commit()
    # Read before the tests run: they expire the session's objects.
    ws_ids = [w.id for w in workspaces]
    user_ids = [user_id, other_id, outsider_id, member_id]

    yield {
        "tag": tag,
        "user_id": user_id,
        "other_id": other_id,
        "member_id": member_id,
        **{f"w{k}": w.id for k, w in zip("abcde", workspaces, strict=True)},
        **{f"w{k}_name": w.name for k, w in zip("abcde", workspaces, strict=True)},
        "a_shared": a_shared.id,
        "a_private": a_private.id,
        "a_dup": a_dup.id,
        "a_deleted": a_deleted.id,
        "b_shared": b_shared.id,
        "b_dup": b_dup.id,
        "b_private_other": b_private_other.id,
        "c_allowed": c_allowed.id,
        "c_hidden": c_hidden.id,
        "d1": d1.id,
        "e1": e1.id,
    }

    try:
        ctx_ids = list(
            (
                await db_session.execute(select(Context.id).where(Context.workspace_id.in_(ws_ids)))
            ).scalars()
        )
        await db_session.execute(
            text(
                "DELETE FROM agent_context_bindings WHERE agent_id IN "
                "(SELECT id FROM agents WHERE workspace_id = ANY(:ws))"
            ),
            {"ws": ws_ids},
        )
        for table in ("agents", "workspace_slot_suspensions"):
            await db_session.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = ANY(:ws)"),  # noqa: S608 - fixed names
                {"ws": ws_ids},
            )
        for table in ("memory_revisions", "memory_access_events", "memories", "usage_stats"):
            await db_session.execute(
                text(f"DELETE FROM {table} WHERE context_id = ANY(:ids)"),  # noqa: S608 - fixed names
                {"ids": ctx_ids},
            )
        await db_session.execute(
            UsageStats.__table__.delete().where(UsageStats.user_id.in_(user_ids))
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
def _clean_request_state():
    from auth.agent_scope import set_agent_scope
    from mcp_server.tools import invalidate_rate_limit_cache
    from mcp_server.tools._helpers import (
        set_mcp_credential_default_context,
        set_mcp_default_context_selection,
        set_mcp_key_workspace_scope,
    )
    from mcp_server.tools._workspace_binding import set_mcp_workspace_binding

    def _reset():
        set_mcp_default_context_selection(None)
        set_mcp_key_workspace_scope(None)
        set_mcp_credential_default_context(None)
        set_mcp_workspace_binding(None)
        set_agent_scope(None)
        invalidate_rate_limit_cache()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _no_vector_collection():
    """Creating a context also creates its vector collection; this suite runs no vector store."""
    from unittest.mock import AsyncMock

    from services.context_service import ContextService

    with patch.object(ContextService, "_ensure_context_collection", AsyncMock()):
        yield


@pytest.fixture
def no_embedding():
    """The post-commit embedding task needs the model provider; skip it."""
    with patch("services.memory_service.MemoryService._schedule_embedding", lambda *_a: None):
        yield


async def _asgi_call(
    monkeypatch,
    user_id: str,
    name: str,
    args: dict,
    *,
    query: bytes = b"profile=core",
    credential_default: UUID | None = None,
    key_workspace: UUID | None = None,
    url_workspace: UUID | None = None,
    agent_id: UUID | None = None,
) -> dict:
    """One stateless ``tools/call`` through ``mcp_asgi_app``; the parsed reply."""
    import mcp_server.transport as transport
    from mcp_server.tools._helpers import set_mcp_credential_default_context
    from tests.mcp_server.test_transport_tool_profiles import (
        _modern_headers,
        _modern_request,
        _receive_for,
        _Recorder,
    )

    async def fake_auth(**_kwargs):
        from auth.agent_scope import AgentScope, set_agent_scope

        set_agent_scope(
            AgentScope(agent_id=agent_id, enforcement_mode="enforce", workspace_id=key_workspace)
            if agent_id is not None
            else None
        )
        set_mcp_credential_default_context(credential_default)
        return user_id, None, key_workspace

    monkeypatch.setattr(transport, "authenticate_mcp_request", fake_auth)

    body = _modern_request("tools/call", name=name, arguments=args)
    send = _Recorder()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "query_string": query,
        "headers": list(_modern_headers(body).items()),
    }
    if url_workspace is not None:
        scope["workspace_id_from_url"] = str(url_workspace)
    await transport.mcp_asgi_app(scope, _receive_for(body), send)
    assert send.status == 200, send.raw
    return json.loads(send.body["result"]["content"][0]["text"])


def _ids(payload: dict) -> set[str]:
    return {item["id"] for item in payload["contexts"]}


def _s(*values) -> set[str]:
    return {str(v) for v in values}


# The contexts the user reaches across A, B and C (D: no seat, E: no membership).
def _reachable(w) -> set[str]:
    return _s(w["a_shared"], w["a_private"], w["a_dup"], w["b_shared"], w["b_dup"], w["c_allowed"])


async def _current_workspace(db_session, user_id):
    db_session.expire_all()
    return (
        await db_session.execute(select(User.current_workspace_id).where(User.user_id == user_id))
    ).scalar_one()


async def _usage_workspaces(db_session, user_id: str, endpoint: str) -> list:
    db_session.expire_all()
    rows = await db_session.execute(
        select(UsageStats.workspace_id).where(
            UsageStats.user_id == user_id, UsageStats.endpoint == endpoint
        )
    )
    return list(rows.scalars())


# =============================================================== #3000: the check


@pytest.mark.asyncio(loop_scope="session")
async def test_the_check_binds_a_reachable_context_and_names_each_refusal(world, db_session):
    from mcp_server.tools._workspace_binding import default_context_workspace

    w = world
    user = w["user_id"]
    cases = {
        "b_shared": (w["wb"], "ok"),
        "c_allowed": (w["wc"], "ok"),
        "e1": (None, "not_a_member"),
        "b_private_other": (None, "private"),
        "c_hidden": (None, "not_reachable"),
        "a_deleted": (None, "not_found"),
    }
    for key, expected in cases.items():
        assert await default_context_workspace(db_session, user, w[key]) == expected, key
    assert await default_context_workspace(db_session, user, uuid4()) == (None, "not_found")


# ========================================================= #3000: end to end


@pytest.mark.asyncio(loop_scope="session")
async def test_a_credential_default_binds_list_create_and_usage_to_its_workspace(
    world, db_session, monkeypatch
):
    """Current workspace A, OAuth grant default in B: everything follows B."""
    w = world
    listed = await _asgi_call(
        monkeypatch, w["user_id"], "list_contexts", {}, credential_default=w["b_shared"]
    )
    assert listed["status"] == "success", listed
    assert _ids(listed) == _s(w["b_shared"], w["b_dup"])
    # A bound listing keeps its shape: no workspace on items, quota figures present.
    assert all("workspace" not in item for item in listed["contexts"])
    assert "limit" in listed and "can_create" in listed

    created = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "create_context",
        {"name": f"made-in-b-{w['tag']}", "is_private": False},
        credential_default=w["b_shared"],
    )
    assert created["status"] == "success", created
    assert created["workspace"] == {"id": str(w["wb"]), "name": w["wb_name"]}

    assert await _usage_workspaces(db_session, w["user_id"], "mcp:list_contexts") == [w["wb"]]
    # The web UI's selection is never written by this path.
    assert await _current_workspace(db_session, w["user_id"]) == w["wa"]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_binding_survives_a_change_of_the_current_workspace(
    world, db_session, monkeypatch
):
    w = world
    await db_session.execute(
        User.__table__.update()
        .where(User.user_id == w["user_id"])
        .values(current_workspace_id=w["wc"])
    )
    await db_session.commit()
    listed = await _asgi_call(
        monkeypatch, w["user_id"], "list_contexts", {}, credential_default=w["b_shared"]
    )
    assert _ids(listed) == _s(w["b_shared"], w["b_dup"])


@pytest.mark.asyncio(loop_scope="session")
async def test_the_url_context_binds_and_wins_over_the_credential(world, monkeypatch):
    w = world
    listed = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "list_contexts",
        {},
        query=f"context={w['c_allowed']}".encode(),
        credential_default=w["b_shared"],
    )
    assert _ids(listed) == _s(w["c_allowed"])


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("default_key", ["e1", "b_private_other", "c_hidden", "a_deleted"])
async def test_a_default_that_fails_the_check_leaves_the_connection_unbound(
    world, db_session, monkeypatch, caplog, default_key
):
    w = world
    with caplog.at_level("INFO", logger="mcp_server.tools._workspace_binding"):
        listed = await _asgi_call(
            monkeypatch, w["user_id"], "list_contexts", {}, credential_default=w[default_key]
        )
    assert listed["status"] == "success", listed
    # Unbound: the listing of every member workspace.
    assert _ids(listed) == _reachable(w)
    lines = [
        r.getMessage() for r in caplog.records if "mcp_workspace_binding_fallback" in r.message
    ]
    assert len(lines) == 1
    assert "source=credential" in lines[0]
    assert str(w[default_key]) not in lines[0]
    assert await _current_workspace(db_session, w["user_id"]) == w["wa"]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_invalid_url_context_never_falls_through_to_the_credential(world, monkeypatch):
    w = world
    listed = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "list_contexts",
        {},
        query=b"context=my-project",
        credential_default=w["b_shared"],
    )
    assert _ids(listed) == _reachable(w)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_user_removed_from_the_workspace_loses_the_binding(world, db_session, monkeypatch):
    w = world
    await db_session.execute(
        WorkspaceMember.__table__.delete().where(
            WorkspaceMember.workspace_id == w["wb"], WorkspaceMember.user_id == w["user_id"]
        )
    )
    await db_session.commit()
    listed = await _asgi_call(
        monkeypatch, w["user_id"], "list_contexts", {}, credential_default=w["b_shared"]
    )
    assert _ids(listed) == _s(w["a_shared"], w["a_private"], w["a_dup"], w["c_allowed"])
    assert all(item["workspace"]["id"] != str(w["wb"]) for item in listed["contexts"])


@pytest.mark.asyncio(loop_scope="session")
async def test_a_key_keeps_its_workspace_whatever_the_default_context(world, monkeypatch):
    """Precedence 2: a workspace-scoped key for A with a default context in B.

    The listing is A's, and the default context stays out of reach (#963), as before.
    """
    w = world
    listed = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "list_contexts",
        {},
        query=f"context={w['b_shared']}".encode(),
        key_workspace=w["wa"],
    )
    assert _ids(listed) == _s(w["a_shared"], w["a_private"], w["a_dup"])
    info = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "get_context_info",
        {},
        query=f"context={w['b_shared']}".encode(),
        key_workspace=w["wa"],
    )
    assert info["error"] == "context_not_found", info
    # Nor can workspace_id or create_context take the key out of A.
    other = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "list_contexts",
        {"workspace_id": str(w["wb"])},
        key_workspace=w["wa"],
    )
    assert other["error"] == "permission_denied", other
    created = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "create_context",
        {"name": f"escape-{w['tag']}", "is_private": False, "workspace_id": str(w["wb"])},
        key_workspace=w["wa"],
    )
    assert created["error"] == "permission_denied", created


@pytest.mark.asyncio(loop_scope="session")
async def test_the_url_workspace_keeps_precedence_over_the_default_context(world, monkeypatch):
    """Precedence 1: /mcp/w/A with a default context in B lists A, and a call
    without context_id still reaches the default context, as before."""
    w = world
    listed = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "list_contexts",
        {},
        credential_default=w["b_shared"],
        url_workspace=w["wa"],
    )
    assert _ids(listed) == _s(w["a_shared"], w["a_private"], w["a_dup"])
    info = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "get_context_info",
        {},
        credential_default=w["b_shared"],
        url_workspace=w["wa"],
    )
    assert info["status"] == "success", info
    assert info["context"]["id"] == str(w["b_shared"])
    mismatch = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "create_context",
        {"name": f"elsewhere-{w['tag']}", "is_private": False, "workspace_id": str(w["wb"])},
        url_workspace=w["wa"],
    )
    assert mismatch["error"] == "workspace_mismatch", mismatch


# =================================================== #3001: the unbound listing


async def _list(w, args: dict | None = None, *, binding="unbound", user_key="user_id") -> dict:
    from mcp_server.tools._workspace_binding import set_mcp_workspace_binding
    from mcp_server.tools.context import handle_list_contexts

    set_mcp_workspace_binding(binding)
    result = await handle_list_contexts(args or {}, w[user_key], w["wa"])
    return json.loads(result[0].text)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unbound_listing_covers_every_member_workspace_labelled(world):
    w = world
    payload = await _list(w)
    assert payload["status"] == "success", payload
    assert _ids(payload) == _reachable(w)
    home = {
        str(w[key]): letter
        for letter, keys in {
            "a": ("a_shared", "a_private", "a_dup"),
            "b": ("b_shared", "b_dup"),
            "c": ("c_allowed",),
        }.items()
        for key in keys
    }
    for item in payload["contexts"]:
        letter = home[item["id"]]
        assert item["workspace"] == {"id": str(w[f"w{letter}"]), "name": w[f"w{letter}_name"]}
    assert payload["count"] == 6 and payload["total"] == 6
    for absent in ("limit", "can_create", "other_workspaces", "workspaces_hint", "hint"):
        assert absent not in payload
    # D (no seat) is named, E (not a member) never appears anywhere.
    assert payload["skipped_workspaces"] == [
        {"id": str(w["wd"]), "name": w["wd_name"], "reason": "feature_not_available"}
    ]
    assert str(w["we"]) not in json.dumps(payload)
    assert str(w["b_private_other"]) not in json.dumps(payload)


@pytest.mark.asyncio(loop_scope="session")
async def test_the_unbound_order_is_most_recently_used_first_across_workspaces(world):
    w = world
    payload = await _list(w)
    order = [item["id"] for item in payload["contexts"]]
    assert order == [
        str(w[k]) for k in ("b_shared", "a_shared", "a_private", "b_dup", "a_dup", "c_allowed")
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_unbound_cursor_pages_the_merged_order(world):
    w = world
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        args = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = await _list(w, args)
        assert page["status"] == "success", page
        assert page["count"] == 6
        seen += [item["id"] for item in page["contexts"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert seen == [item["id"] for item in (await _list(w))["contexts"]]
    assert cursor is None


@pytest.mark.asyncio(loop_scope="session")
async def test_a_cursor_of_one_listing_is_refused_by_the_other(world):
    w = world
    cross = (await _list(w, {"limit": 1}))["next_cursor"]
    bound = (await _list(w, {"limit": 1}, binding="default_context"))["next_cursor"]
    assert bound == "1"
    assert (await _list(w, {"cursor": bound}))["error"] == "validation_error"
    assert (await _list(w, {"cursor": cross}, binding="default_context"))["error"] == (
        "validation_error"
    )
    named = await _list(w, {"cursor": cross, "workspace_id": str(w["wb"])})
    assert named["error"] == "validation_error"


@pytest.mark.asyncio(loop_scope="session")
async def test_same_names_in_two_workspaces_are_both_listed(world):
    w = world
    payload = await _list(w, {"name_contains": "DUP"})
    assert _ids(payload) == _s(w["a_dup"], w["b_dup"])
    assert {item["workspace"]["id"] for item in payload["contexts"]} == _s(w["wa"], w["wb"])
    assert payload["count"] == 6  # counted before the filter, as in a bound listing


@pytest.mark.asyncio(loop_scope="session")
async def test_the_detail_flags_work_per_item(world):
    w = world
    payload = await _list(w, {"include_summary": True, "include_stats": True})
    assert all("summary" in item and "memory_count" in item for item in payload["contexts"])


@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_id_narrows_an_unbound_listing_to_today_s_shape(world):
    w = world
    payload = await _list(w, {"workspace_id": str(w["wb"])})
    assert _ids(payload) == _s(w["b_shared"], w["b_dup"])
    assert all("workspace" not in item for item in payload["contexts"])
    assert "skipped_workspaces" not in payload
    outside = await _list(w, {"workspace_id": str(w["we"])})
    assert outside["error"] == "permission_denied"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_bound_listing_is_unchanged(world):
    w = world
    for binding in (None, "default_context", "url", "key"):
        payload = await _list(w, binding=binding)
        assert _ids(payload) == _s(w["a_shared"], w["a_private"], w["a_dup"]), binding
        assert all("workspace" not in item for item in payload["contexts"])
        assert payload["limit"] > 0 and "skipped_workspaces" not in payload
        assert payload["other_workspaces"] == 2  # B and C; D has no seat


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unbound_listing_goes_through_the_dispatcher_whatever_the_current_workspace(
    world, db_session, monkeypatch
):
    """The current workspace is no gate on the merged listing: a user with no
    current workspace still gets every member workspace."""
    w = world
    await db_session.execute(
        User.__table__.update()
        .where(User.user_id == w["user_id"])
        .values(current_workspace_id=None)
    )
    await db_session.commit()
    listed = await _asgi_call(monkeypatch, w["user_id"], "list_contexts", {})
    assert _ids(listed) == _reachable(w)


# ============================== #3001 B4/B5: contexts of other workspaces by id


@pytest.mark.asyncio(loop_scope="session")
async def test_listed_contexts_of_another_workspace_work_by_id_on_an_unbound_connection(
    world, db_session, monkeypatch, no_embedding
):
    """Current workspace A, a context of B: the context-scoped tools resolve it
    by id under the same permission checks, and usage counts in B."""
    w = world
    b_ctx = str(w["b_shared"])
    info = await _asgi_call(monkeypatch, w["user_id"], "get_context_info", {"context_id": b_ctx})
    assert info["status"] == "success", info
    stored = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "remember",
        {"context_id": b_ctx, "summary": "Cross workspace note", "content": "body", "type": "note"},
    )
    assert stored["status"] == "success", stored
    memory_id = stored["memory_id"]
    listed = await _asgi_call(monkeypatch, w["user_id"], "list", {"context_id": b_ctx})
    assert memory_id in json.dumps(listed)
    updated = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "update_memory",
        {"context_id": b_ctx, "memory_id": memory_id, "summary": "Cross workspace note, edited"},
    )
    assert updated["status"] == "success", updated
    boot = await _asgi_call(monkeypatch, w["user_id"], "bootstrap", {"context_id": b_ctx})
    assert boot["status"] == "success", boot
    forgotten = await _asgi_call(
        monkeypatch, w["user_id"], "forget", {"context_id": b_ctx, "memory_id": memory_id}
    )
    assert forgotten["status"] == "success", forgotten

    # B5: every call on B's context is counted in B, not in the connection's A.
    for endpoint in ("mcp:get_context_info", "mcp:remember", "mcp:update_memory", "mcp:forget"):
        assert set(await _usage_workspaces(db_session, w["user_id"], endpoint)) == {w["wb"]}, (
            endpoint
        )
    # A context of a workspace the user is not a member of is never credited there.
    denied = await _asgi_call(
        monkeypatch, w["user_id"], "get_context_info", {"context_id": str(w["e1"])}
    )
    assert denied["error"] == "context_not_found"
    assert w["we"] not in await _usage_workspaces(db_session, w["user_id"], "mcp:get_context_info")


@pytest.mark.asyncio(loop_scope="session")
async def test_the_shared_resolvers_reach_another_member_workspace(world, db_session):
    """recall / reference / the other read tools resolve through these two
    (the vector store is not run here): a context of B resolves from any
    session, a private context of another member and a non-member's do not."""
    from mcp_server.tools._helpers import (
        _ContextNotFoundError,
        _resolve_context,
        _resolve_context_for_read,
        set_mcp_capacity_policy,
    )
    from services.capacity_lock import CapacityPolicy

    w = world
    set_mcp_capacity_policy(CapacityPolicy.ALLOWED)
    for resolve in (_resolve_context, _resolve_context_for_read):
        context = await resolve(db_session, w["user_id"], w["b_shared"])
        assert context.workspace_id == w["wb"]
        for key in ("b_private_other", "e1", "c_hidden"):
            with pytest.raises(_ContextNotFoundError):
                await resolve(db_session, w["user_id"], w[key])
        await db_session.rollback()


# ======================================================= #3002: create_context


async def _create(w, args: dict, *, binding="unbound", user_key="user_id") -> dict:
    from mcp_server.tools._workspace_binding import set_mcp_workspace_binding
    from mcp_server.tools.context import handle_create_context

    set_mcp_workspace_binding(binding)
    result = await handle_create_context(
        {"name": f"new-{uuid4().hex[:8]}"} | args, w[user_key], w["wa"]
    )
    return json.loads(result[0].text)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unbound_private_create_goes_to_the_only_workspace_that_allows_it(world):
    """Private: only an owner may create one, and the user owns A only."""
    w = world
    created = await _create(w, {})
    assert created["status"] == "success", created
    assert created["workspace"] == {"id": str(w["wa"]), "name": w["wa_name"]}


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unbound_shared_create_with_two_candidates_needs_workspace_id(world, db_session):
    w = world
    refused = await _create(w, {"is_private": False})
    assert refused["error"] == "workspace_required", refused
    assert "workspace_id" in refused["message"]
    assert refused["candidates"] == [
        {"id": str(w["wa"]), "name": w["wa_name"], "role": "owner"},
        {"id": str(w["wb"]), "name": w["wb_name"], "role": "admin"},
    ]
    count = (
        await db_session.execute(
            select(Context.id).where(Context.workspace_id.in_([w["wa"], w["wb"]]))
        )
    ).all()
    assert len(count) == 7  # nothing created


@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_id_creates_there_under_the_usual_checks(world):
    w = world
    created = await _create(w, {"is_private": False, "workspace_id": str(w["wb"])})
    assert created["status"] == "success", created
    assert created["workspace"] == {"id": str(w["wb"]), "name": w["wb_name"]}
    # An admin creates shared contexts only (ContextService's rule).
    private = await _create(w, {"workspace_id": str(w["wb"])})
    assert private["status"] != "success"
    member = await _create(w, {"is_private": False, "workspace_id": str(w["wc"])})
    assert member["error"] == "permission_denied" and member["your_role"] == "member"
    outsider = await _create(w, {"is_private": False, "workspace_id": str(w["we"])})
    assert outsider["error"] == "permission_denied"
    assert outsider["your_role"] == "not_a_member"
    malformed = await _create(w, {"workspace_id": "not-a-uuid"})
    assert malformed["error"] == "invalid_argument"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_bound_create_uses_the_connection_workspace(world):
    w = world
    for binding in (None, "default_context"):
        created = await _create(w, {"is_private": False}, binding=binding)
        assert created["status"] == "success", (binding, created)
        assert created["workspace"]["id"] == str(w["wa"])


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unbound_caller_who_can_create_nowhere_gets_today_s_refusal(world):
    w = world
    refused = await _create(w, {"is_private": False}, user_key="member_id")
    assert refused["error"] == "permission_denied", refused
    assert refused["your_role"] == "member"
    assert "candidates" not in refused


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unbound_create_through_the_dispatcher_lands_in_the_picked_workspace(
    world, monkeypatch
):
    """The dispatcher's session-workspace gates do not stand in for the
    handler's: with no current workspace the one creatable workspace is used."""
    w = world
    created = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "create_context",
        {"name": f"via-dispatch-{w['tag']}"},
    )
    assert created["status"] == "success", created
    assert created["workspace"]["id"] == str(w["wa"])


# ============================================= review fixes: metering and scope


_NOTE = {"summary": "Metering note", "content": "body", "type": "note"}


async def _cap_workspace_calls(db_session, workspace_id, limit: int, user_id: str) -> None:
    """Hold the workspace's daily MCP call limit at ``limit`` and use it all today."""
    from utils.datetime import utcnow

    workspace = await db_session.get(Workspace, workspace_id)
    base = workspace.effective_mcp_calls_per_day - (workspace.addon_mcp_quota_bonus or 0)
    workspace.addon_mcp_quota_bonus = limit - base
    now = utcnow()
    db_session.add_all(
        [
            UsageStats(
                user_id=user_id,
                endpoint="mcp:list",
                method="MCP",
                status_code=200,
                created_at=now,
                date=now.date(),
                workspace_id=workspace_id,
            )
            for _ in range(limit)
        ]
    )
    await db_session.commit()
    assert workspace.effective_mcp_calls_per_day == limit


@pytest.mark.asyncio(loop_scope="session")
async def test_the_daily_limit_is_the_workspace_of_the_context_the_call_names(
    world, db_session, monkeypatch, no_embedding
):
    """Unbound connection in A; B has used its whole day. A call on a B context
    is refused by B's limit, a call on an A context still passes, and the
    usage row goes where the check looked."""
    w = world
    await _cap_workspace_calls(db_session, w["wb"], 2, w["other_id"])

    refused = await _asgi_call(
        monkeypatch, w["user_id"], "remember", {"context_id": str(w["b_shared"]), **_NOTE}
    )
    assert refused["error"] == "rate_limit_exceeded", refused
    assert refused["daily_limit"] == 2

    passed = await _asgi_call(
        monkeypatch, w["user_id"], "remember", {"context_id": str(w["a_shared"]), **_NOTE}
    )
    assert passed["status"] == "success", passed
    assert await _usage_workspaces(db_session, w["user_id"], "mcp:remember") == [w["wa"]]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_default_context_call_is_charged_to_the_default_context_workspace(
    world, db_session, monkeypatch, no_embedding
):
    """Under /mcp/w/A a call without context_id runs in the default context of
    B; it is checked against, and counted in, B."""
    w = world
    listed = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "remember",
        dict(_NOTE),
        credential_default=w["b_shared"],
        url_workspace=w["wa"],
    )
    assert listed["status"] == "success", listed
    assert await _usage_workspaces(db_session, w["user_id"], "mcp:remember") == [w["wb"]]
    await _cap_workspace_calls(db_session, w["wb"], 1, w["other_id"])
    from mcp_server.tools import invalidate_rate_limit_cache

    invalidate_rate_limit_cache()
    refused = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "remember",
        dict(_NOTE),
        credential_default=w["b_shared"],
        url_workspace=w["wa"],
    )
    assert refused["error"] == "rate_limit_exceeded", refused


@pytest.mark.asyncio(loop_scope="session")
async def test_a_key_never_writes_a_usage_row_into_another_workspace(
    world, db_session, monkeypatch
):
    """Key scoped to A, user also in B: a refused call naming a B context
    (logged with the raw context_id) still counts in A."""
    w = world
    for name, args in (
        ("list_revisions", {"context_id": str(w["b_shared"]), "memory_id": str(uuid4())}),
        ("rollback_session", {"context_id": str(w["b_shared"]), "session_id": "s-1"}),
        ("get_context_info", {"context_id": str(w["b_shared"])}),
    ):
        reply = await _asgi_call(monkeypatch, w["user_id"], name, args, key_workspace=w["wa"])
        assert reply["status"] != "success", (name, reply)
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(UsageStats.workspace_id).where(UsageStats.user_id == w["user_id"])
            )
        )
        .scalars()
        .all()
    )
    assert rows, "the refused calls are still counted"
    assert set(rows) == {w["wa"]}


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unbound_listing_names_a_suspended_workspace(world, db_session):
    from models.workspace_slots import WorkspaceSlotSuspension

    w = world
    db_session.add(
        WorkspaceSlotSuspension(
            workspace_id=w["wc"],
            owner_user_id=w["other_id"],
            cause="billing_snapshot",
            suspended_at=datetime(2031, 5, 1, 12, 0, 0),
        )
    )
    await db_session.commit()
    with patch("services.workspace_slots.workspace_slot_mode", return_value="enforce"):
        payload = await _list(w)
    assert payload["status"] == "success", payload
    assert _ids(payload) == _reachable(w) - _s(w["c_allowed"])
    assert {"id": str(w["wc"]), "name": w["wc_name"], "reason": "workspace_suspended"} in payload[
        "skipped_workspaces"
    ]


@pytest_asyncio.fixture(loop_scope="session")
async def agent_in_a(world, db_session):
    """An agent of the user in A, bound to ``a_shared`` only (enforce mode)."""
    from models.agent import Agent
    from services.agent_binding_service import AgentBindingService

    w = world
    agent = Agent(workspace_id=w["wa"], name=f"wb-agent-{w['tag']}", owner_user_id=w["user_id"])
    db_session.add(agent)
    await db_session.flush()
    await AgentBindingService(db_session).create_binding(
        agent=agent,
        context_id=w["a_shared"],
        created_by=w["user_id"],
        can_read=True,
        write_policy="direct",
        is_default=True,
    )
    await db_session.commit()
    return agent.id


@pytest.mark.asyncio(loop_scope="session")
async def test_an_agent_bound_key_stays_in_its_workspace_and_binding(
    world, agent_in_a, monkeypatch
):
    w = world
    call = {"key_workspace": w["wa"], "agent_id": agent_in_a}
    listed = await _asgi_call(monkeypatch, w["user_id"], "list_contexts", {}, **call)
    assert _ids(listed) == _s(w["a_shared"])  # the binding still narrows the listing
    assert all("workspace" not in item for item in listed["contexts"])
    other = await _asgi_call(
        monkeypatch, w["user_id"], "list_contexts", {"workspace_id": str(w["wb"])}, **call
    )
    assert other["error"] == "permission_denied", other
    url_default = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "get_context_info",
        {},
        query=f"context={w['b_shared']}".encode(),
        **call,
    )
    assert url_default["error"] == "context_not_found", url_default
    created = await _asgi_call(
        monkeypatch,
        w["user_id"],
        "create_context",
        {"name": f"agent-escape-{w['tag']}", "is_private": False, "workspace_id": str(w["wb"])},
        **call,
    )
    assert created["error"] == "permission_denied", created


# ===================================== legacy sessions follow the workspace


async def _legacy_post(monkeypatch, user_id: str, body: dict, session_id: str | None, **kw):
    import mcp_server.transport as transport
    from mcp_server.tools._helpers import set_mcp_credential_default_context
    from tests.mcp_server.test_transport_tool_profiles import _receive_for, _Recorder

    async def fake_auth(**_kwargs):
        set_mcp_credential_default_context(kw.get("credential_default"))
        return user_id, None, None

    monkeypatch.setattr(transport, "authenticate_mcp_request", fake_auth)
    headers = [(b"content-type", b"application/json"), (b"accept", b"application/json")]
    if session_id:
        headers.append((b"mcp-session-id", session_id.encode()))
    send = _Recorder()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "query_string": b"",
        "headers": headers,
    }
    await transport.mcp_asgi_app(scope, _receive_for(body), send)
    return send


@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_re_initializes_when_its_workspace_binding_changes(
    world, db_session, monkeypatch
):
    """A session opened while the default context bound it to B is the
    caller's own only while that holds; once the user leaves B the session
    reads as another workspace's and is answered 404, as for a change of the
    current workspace, and the client opens a new one."""
    w = world
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t"},
        },
    }
    opened = await _legacy_post(
        monkeypatch, w["user_id"], init, None, credential_default=w["b_shared"]
    )
    assert opened.status == 200
    session_id = opened.headers[b"mcp-session-id"].decode()
    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
    same = await _legacy_post(
        monkeypatch, w["user_id"], ping, session_id, credential_default=w["b_shared"]
    )
    assert same.status == 200

    await db_session.execute(
        WorkspaceMember.__table__.delete().where(
            WorkspaceMember.workspace_id == w["wb"], WorkspaceMember.user_id == w["user_id"]
        )
    )
    await db_session.commit()
    gone = await _legacy_post(
        monkeypatch, w["user_id"], ping, session_id, credential_default=w["b_shared"]
    )
    assert gone.status == 404
