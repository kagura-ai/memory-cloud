"""#2972: ``list_workspaces`` and ``list_contexts(workspace_id=...)``.

An account in several workspaces finds the others from the connection it has:
``list_workspaces`` (full profile, callable from any URL) names them, and
``list_contexts(workspace_id=...)`` lists one under the visibility a connection
to that workspace has. ``list_contexts`` points at both when the account has
more than one workspace. A workspace-scoped API key stays in its workspace.

Against a real database and the real ``PermissionService``.
"""

import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, User, Workspace, WorkspaceMember

# The keys of a list_contexts reply before #2972 (no pointer).
_REPLY_KEYS = {"status", "contexts", "count", "total", "limit", "can_create", "has_more"}
_REPLY_KEYS |= {"next_cursor"}


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
async def accounts(async_engine, db_session, monkeypatch):
    """``user``: owner of A, member of B with ``b_shared`` on its list (connection on B).

    - A: ``a_shared`` (the user's), ``a_private_other`` (another member's private one)
    - B: ``b_shared``
    - C: ``c1``; ``solo`` is a member of C only
    - D: a one-seat (free) workspace of ``other`` where ``user`` is a member
      without a seat
    - ``other``: owner of B; in A a member with ``a_shared`` on its list, whose
      private context the user must not see
    """
    import db.base as _db_base

    monkeypatch.setattr(
        _db_base,
        "async_session_factory",
        async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False),
    )

    tag = uuid4().hex[:8]
    user_id, other_id, solo_id = f"lws-user-{tag}", f"lws-other-{tag}", f"lws-solo-{tag}"
    wa = _workspace(tag, "lws-a", user_id)
    wb = _workspace(tag, "lws-b", other_id)
    wc = _workspace(tag, "lws-c", solo_id)
    wd = _workspace(tag, "lws-d", other_id, plan_name="free")
    db_session.add_all([wa, wb, wc, wd])
    await db_session.flush()
    db_session.add_all([_user(user_id, wb.id), _user(other_id, wb.id), _user(solo_id, wc.id)])
    await db_session.flush()

    def _ctx(workspace: Workspace, name: str, created_by: str, **kwargs) -> Context:
        return Context(
            id=uuid4(),
            workspace_id=workspace.id,
            name=f"{name}-{tag}",
            created_by=created_by,
            **kwargs,
        )

    a_shared = _ctx(wa, "a-shared", user_id, is_private=False)
    a_private_other = _ctx(wa, "a-private-other", other_id, is_private=True)
    b_shared = _ctx(wb, "b-shared", other_id, is_private=False)
    c1 = _ctx(wc, "c1", solo_id, is_private=False)
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=wa.id, user_id=user_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(
                workspace_id=wa.id,
                user_id=other_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[a_shared.id],
            ),
            WorkspaceMember(workspace_id=wb.id, user_id=other_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(
                workspace_id=wb.id,
                user_id=user_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[b_shared.id],
            ),
            WorkspaceMember(workspace_id=wc.id, user_id=solo_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=wd.id, user_id=other_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=wd.id, user_id=user_id, role=WorkspaceRole.MEMBER),
            a_shared,
            a_private_other,
            b_shared,
            c1,
        ]
    )
    await db_session.commit()

    yield {
        "user_id": user_id,
        "other_id": other_id,
        "solo_id": solo_id,
        "wa": wa.id,
        "wb": wb.id,
        "wc": wc.id,
        "wd": wd.id,
        "wa_name": wa.name,
        "wb_name": wb.name,
        "a_shared": a_shared.id,
        "a_private_other": a_private_other.id,
        "b_shared": b_shared.id,
        "c1": c1.id,
    }

    try:
        workspace_ids = [wa.id, wb.id, wc.id, wd.id]
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(
                WorkspaceMember.workspace_id.in_(workspace_ids)
            )
        )
        await db_session.execute(
            Context.__table__.delete().where(Context.workspace_id.in_(workspace_ids))
        )
        await db_session.execute(
            User.__table__.delete().where(User.user_id.in_([user_id, other_id, solo_id]))
        )
        await db_session.execute(
            Workspace.__table__.delete().where(Workspace.id.in_(workspace_ids))
        )
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


@contextmanager
def _key_scope(workspace_id):
    from mcp_server.tools._helpers import set_mcp_key_workspace_scope

    set_mcp_key_workspace_scope(workspace_id)
    try:
        yield
    finally:
        set_mcp_key_workspace_scope(None)


@contextmanager
def _view(query: bytes):
    from mcp_server.tools._helpers import set_mcp_tool_view_query

    set_mcp_tool_view_query(query)
    try:
        yield
    finally:
        set_mcp_tool_view_query(None)


async def _contexts(s, args: dict, *, user_key: str = "user_id", connection: str = "wb") -> dict:
    from mcp_server.tools.context import handle_list_contexts

    result = await handle_list_contexts(args, s[user_key], s[connection])
    return json.loads(result[0].text)


async def _workspaces(s, *, user_key: str = "user_id", connection: str = "wb") -> dict:
    from mcp_server.tools.workspaces import handle_list_workspaces

    result = await handle_list_workspaces({}, s[user_key], s[connection])
    return json.loads(result[0].text)


def _ids(payload: dict) -> set[str]:
    return {item["id"] for item in payload["contexts"]}


# --------------------------------------------------------------- list_workspaces


@pytest.mark.asyncio(loop_scope="session")
async def test_list_workspaces_lists_both_and_marks_the_connection(accounts):
    s = accounts
    payload = await _workspaces(s)

    assert payload["status"] == "success", payload
    by_id = {item["id"]: item for item in payload["workspaces"]}
    assert set(by_id) == {str(s["wa"]), str(s["wb"]), str(s["wd"])}
    assert by_id[str(s["wa"])] == {
        "id": str(s["wa"]),
        "name": s["wa_name"],
        "role": "owner",
        "current": False,
        "usable": True,
    }
    assert by_id[str(s["wb"])] == {
        "id": str(s["wb"]),
        "name": s["wb_name"],
        "role": "member",
        "current": True,
        "usable": True,
    }
    assert payload["total"] == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_a_workspace_without_a_seat_is_listed_as_not_usable_and_not_counted(accounts):
    """list_workspaces, the pointer and list_contexts(workspace_id) agree: the
    seatless member's workspace is not usable, list_contexts refuses it, and
    the pointer counts only A (usable) besides the connection's B."""
    s = accounts
    workspaces = await _workspaces(s)
    refused = await _contexts(s, {"workspace_id": str(s["wd"])})
    plain = await _contexts(s, {})

    seatless = next(i for i in workspaces["workspaces"] if i["id"] == str(s["wd"]))
    assert seatless["usable"] is False
    assert seatless["reason"] == "feature_not_available"
    assert refused["error"] == "feature_not_available", refused
    assert plain["other_workspaces"] == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_list_workspaces_under_a_workspace_key_returns_that_workspace_only(accounts):
    s = accounts
    with _key_scope(s["wb"]):
        payload = await _workspaces(s)

    assert [item["id"] for item in payload["workspaces"]] == [str(s["wb"])]
    assert payload["workspaces"][0]["current"] is True
    assert payload["workspaces"][0]["usable"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_list_workspaces_is_not_in_core_but_callable_from_a_core_url(accounts):
    from mcp_server.tools import execute_tool_call
    from mcp_server.tools._profiles import tool_view_names

    s = accounts
    assert "list_workspaces" not in tool_view_names(b"profile=core")
    assert "list_workspaces" not in tool_view_names(None)
    assert "list_workspaces" in tool_view_names(b"profile=full")

    with _view(b"profile=core"):
        result = await execute_tool_call("list_workspaces", {}, s["user_id"], s["wb"])
    payload = json.loads(result[0].text)
    assert payload["status"] == "success", payload
    assert payload["total"] == 3


# ------------------------------------------------- list_contexts(workspace_id=...)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_contexts_of_another_workspace_by_argument(accounts):
    s = accounts
    payload = await _contexts(s, {"workspace_id": str(s["wa"])})

    assert payload["status"] == "success", payload
    # A's contexts the user can see; the other member's private one stays hidden.
    assert _ids(payload) == {str(s["a_shared"])}
    assert str(s["a_private_other"]) not in _ids(payload)
    assert payload["count"] == 1
    # create_context creates in the connection's workspace (B), so nothing
    # about creating in A is offered.
    assert "limit" not in payload and "can_create" not in payload
    assert "hint" not in payload
    assert payload["other_workspaces"] == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_list_contexts_of_another_workspace_applies_the_role_there(accounts):
    """``other`` is the owner of B and, in A, a member whose access list holds
    ``a_shared`` only: in A it sees that one (not even its own private
    context), exactly as a connection to A would."""
    s = accounts
    in_a = await _contexts(s, {"workspace_id": str(s["wa"])}, user_key="other_id")
    on_a = await _contexts(s, {}, user_key="other_id", connection="wa")

    assert _ids(in_a) == _ids(on_a) == {str(s["a_shared"])}
    assert in_a["count"] == on_a["count"]


@pytest.mark.asyncio(loop_scope="session")
async def test_list_contexts_naming_the_connection_workspace_lists_it(accounts):
    s = accounts
    named = await _contexts(s, {"workspace_id": str(s["wb"])})
    omitted = await _contexts(s, {})
    # Some clients send every unset optional argument as null or "".
    null = await _contexts(s, {"workspace_id": None})
    blank = await _contexts(s, {"workspace_id": ""})

    assert named == omitted == null == blank
    assert _ids(named) == {str(s["b_shared"])}


@pytest.mark.asyncio(loop_scope="session")
async def test_list_contexts_pointer_for_an_account_in_two_workspaces(accounts):
    s = accounts
    payload = await _contexts(s, {})

    assert _ids(payload) == {str(s["b_shared"])}
    assert payload["other_workspaces"] == 1
    assert "list_workspaces" in payload["workspaces_hint"]
    assert "workspace_id" in payload["workspaces_hint"]
    assert set(payload) == _REPLY_KEYS | {"other_workspaces", "workspaces_hint"}


@pytest.mark.asyncio(loop_scope="session")
async def test_list_contexts_pointer_on_the_google_view_is_the_count_only(accounts):
    from mcp_server.tools import execute_tool_call

    s = accounts
    with _view(b"profile=google"):
        result = await execute_tool_call("list_contexts", {}, s["user_id"], s["wb"])
    payload = json.loads(result[0].text)

    assert payload["other_workspaces"] == 1
    assert "workspaces_hint" not in payload
    assert "list_workspaces" not in result[0].text


@pytest.mark.asyncio(loop_scope="session")
async def test_list_contexts_for_an_account_in_one_workspace_has_no_pointer(accounts):
    s = accounts
    payload = await _contexts(s, {}, user_key="solo_id", connection="wc")

    assert _ids(payload) == {str(s["c1"])}
    assert set(payload) == _REPLY_KEYS


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("target", ["wc", None])
async def test_list_contexts_of_a_workspace_the_account_is_not_in_is_refused(accounts, target):
    s = accounts
    workspace_id = str(s[target]) if target else str(uuid4())
    payload = await _contexts(s, {"workspace_id": workspace_id})

    assert payload["error"] == "permission_denied", payload
    assert payload["your_role"] == "not_a_member"
    assert "contexts" not in payload
    assert str(s["c1"]) not in json.dumps(payload)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("bad", ["not-a-uuid", 42, "   "])
async def test_list_contexts_with_a_malformed_workspace_id_is_an_invalid_argument(accounts, bad):
    s = accounts
    payload = await _contexts(s, {"workspace_id": bad})

    assert payload["error"] == "invalid_argument", payload


@pytest.mark.asyncio(loop_scope="session")
async def test_a_workspace_key_lists_no_other_workspace_and_gets_no_pointer(accounts):
    s = accounts
    with _key_scope(s["wb"]):
        other = await _contexts(s, {"workspace_id": str(s["wa"])})
        outsider = await _contexts(s, {"workspace_id": str(s["wc"])})
        own = await _contexts(s, {"workspace_id": str(s["wb"])})
        plain = await _contexts(s, {})

    # Refused like a workspace the account is not in: the two cannot be told apart.
    assert other == outsider
    assert other["error"] == "permission_denied"
    assert _ids(own) == _ids(plain) == {str(s["b_shared"])}
    assert set(plain) == _REPLY_KEYS
    assert set(own) == _REPLY_KEYS


@pytest.mark.asyncio(loop_scope="session")
async def test_an_empty_other_workspace_gets_no_create_context_hint(accounts, db_session):
    """A member of C who sees nothing there is told where contexts of C are
    created (a connection to C), not to call create_context, which would
    create in the connection's workspace."""
    s = accounts
    db_session.add(
        WorkspaceMember(
            workspace_id=s["wc"],
            user_id=s["user_id"],
            role=WorkspaceRole.MEMBER,
            allowed_context_ids=[],
        )
    )
    await db_session.commit()

    payload = await _contexts(s, {"workspace_id": str(s["wc"])})

    assert payload["status"] == "success", payload
    assert payload["contexts"] == []
    assert "limit" not in payload and "can_create" not in payload
    assert "create_context" not in payload["hint"]
    assert f"/mcp/w/{s['wc']}" in payload["hint"]
