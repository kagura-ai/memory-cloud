"""#2884: MCP ``list_contexts`` lists the workspace the request is bound to.

The transport binds every MCP request to one workspace: the API key's scope,
else the workspace in the URL, else the user's current workspace. The listing,
``count`` and ``limit`` all come from that workspace, against a real database
and the real ``PermissionService``.
"""

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, User, Workspace, WorkspaceMember


def _workspace(tag: str, name: str, owner_id: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"{name}-{tag}",
        plan_name="pro",
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
async def two_workspaces(async_engine, db_session, monkeypatch):
    """One user who owns A and B and has B selected; a restricted member of A.

    - A: ``a1`` and ``a2`` (the user's), ``a_private`` (the other member's private one)
    - B: ``b1`` (the user's)
    - ``other``: a member of A whose access list holds only ``a1``, and an admin
      of B, with B selected
    - C: ``c1``, a workspace the user does not belong to
    """
    import db.base as _db_base

    # MCP handlers open their own session via get_db(); point it at the test engine.
    monkeypatch.setattr(
        _db_base,
        "async_session_factory",
        async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False),
    )

    tag = uuid4().hex[:8]
    user_id, other_id, outsider_id = f"lw-user-{tag}", f"lw-other-{tag}", f"lw-out-{tag}"
    wa = _workspace(tag, "lw-a", user_id)
    wb = _workspace(tag, "lw-b", user_id)
    wc = _workspace(tag, "lw-c", outsider_id)
    db_session.add_all([wa, wb, wc])
    await db_session.flush()
    db_session.add_all([_user(user_id, wb.id), _user(other_id, wb.id), _user(outsider_id, wc.id)])
    await db_session.flush()

    def _ctx(workspace: Workspace, name: str, created_by: str, **kwargs) -> Context:
        return Context(
            id=uuid4(),
            workspace_id=workspace.id,
            name=f"{name}-{tag}",
            created_by=created_by,
            **kwargs,
        )

    # Shared (not private), so a member other than the creator can be given them.
    a1 = _ctx(wa, "a1", user_id, is_private=False)
    a2 = _ctx(wa, "a2", user_id)
    a_private = _ctx(wa, "a-private", other_id, is_private=True)
    b1 = _ctx(wb, "b1", user_id, is_private=False)
    c1 = _ctx(wc, "c1", outsider_id)
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=wa.id, user_id=user_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(
                workspace_id=wa.id,
                user_id=other_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[a1.id],
            ),
            WorkspaceMember(workspace_id=wb.id, user_id=other_id, role=WorkspaceRole.ADMIN),
            WorkspaceMember(workspace_id=wb.id, user_id=user_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=wc.id, user_id=outsider_id, role=WorkspaceRole.OWNER),
            a1,
            a2,
            a_private,
            b1,
            c1,
        ]
    )
    await db_session.commit()

    yield {
        "user_id": user_id,
        "other_id": other_id,
        "wa": wa.id,
        "wb": wb.id,
        "wc": wc.id,
        "a1": a1.id,
        "a2": a2.id,
        "a_private": a_private.id,
        "b1": b1.id,
        "c1": c1.id,
    }

    # Teardown — FK order: members → contexts → users → workspaces.
    try:
        workspace_ids = [wa.id, wb.id, wc.id]
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(
                WorkspaceMember.workspace_id.in_(workspace_ids)
            )
        )
        await db_session.execute(
            Context.__table__.delete().where(Context.workspace_id.in_(workspace_ids))
        )
        await db_session.execute(
            User.__table__.delete().where(User.user_id.in_([user_id, other_id, outsider_id]))
        )
        await db_session.execute(
            Workspace.__table__.delete().where(Workspace.id.in_(workspace_ids))
        )
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


async def _list(
    scenario, workspace_key: str | None, args: dict | None = None, *, user_key: str = "user_id"
) -> dict:
    from mcp_server.tools.context import handle_list_contexts

    workspace_id = scenario[workspace_key] if workspace_key else None
    result = await handle_list_contexts(args or {}, scenario[user_key], workspace_id)
    return json.loads(result[0].text)


def _ids(payload: dict) -> set[str]:
    return {item["id"] for item in payload["contexts"]}


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "extra", [{}, {"include_summary": True}, {"include_details": True}, {"include_stats": True}]
)
async def test_bound_workspace_is_listed_not_the_selected_one(two_workspaces, extra):
    s = two_workspaces
    payload = await _list(s, "wa", extra)

    assert payload["status"] == "success", payload
    # B is the user's selected workspace, but the request is bound to A.
    assert _ids(payload) == {str(s["a1"]), str(s["a2"])}
    assert payload["total"] == 2
    # count / limit describe the same workspace the items come from (all of A,
    # including the private context the caller cannot see).
    assert payload["count"] == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_selected_workspace_is_listed_when_the_request_is_bound_to_it(two_workspaces):
    s = two_workspaces
    payload = await _list(s, "wb")

    assert _ids(payload) == {str(s["b1"])}
    assert payload["count"] == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_name_filter_stays_inside_the_bound_workspace(two_workspaces):
    s = two_workspaces
    # "b1-" matches only the context in B, the selected workspace.
    payload = await _list(s, "wa", {"name_contains": "b1-"})

    assert payload["status"] == "success", payload
    assert payload["contexts"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_a_workspace_the_user_does_not_belong_to_lists_nothing(two_workspaces):
    s = two_workspaces
    payload = await _list(s, "wc")

    assert payload["status"] == "success", payload
    assert payload["contexts"] == []
    assert str(s["c1"]) not in _ids(payload)
    # Not an empty account: a failed lookup gets no onboarding hint, and no
    # quota figures of a workspace the caller cannot list.
    assert "hint" not in payload
    assert payload["count"] == 0
    assert "limit" not in payload


@pytest.mark.asyncio(loop_scope="session")
async def test_no_bound_workspace_falls_back_to_the_selected_one(two_workspaces):
    s = two_workspaces
    payload = await _list(s, None)

    assert _ids(payload) == {str(s["b1"])}


@pytest.mark.asyncio(loop_scope="session")
async def test_role_and_access_list_come_from_the_bound_workspace(two_workspaces):
    s = two_workspaces
    # An admin of B (selected), but in A a member whose access list holds a1 only.
    in_a = await _list(s, "wa", user_key="other_id")
    in_b = await _list(s, "wb", user_key="other_id")

    # Not a2 (off the list), and not the member's own private context either.
    assert _ids(in_a) == {str(s["a1"])}
    assert _ids(in_b) == {str(s["b1"])}
