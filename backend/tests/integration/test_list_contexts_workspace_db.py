"""#2884: MCP ``list_contexts`` lists the workspace the request is bound to.

The transport binds every MCP request to one workspace: the API key's scope,
else the workspace in the URL, else the user's current workspace. The listing,
``count``, ``limit`` and ``can_create`` all come from that workspace, against a
real database and the real ``PermissionService``.

#2891: ``count`` is the number of contexts the caller can see in that
workspace; ``limit`` / ``can_create`` are decided on every context of it
(private ones of other members included), and a full workspace whose caller
sees fewer contexts than ``limit`` says so in ``hint``.
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
        "outsider_id": outsider_id,
        "tag": tag,
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


async def _cap(db_session: AsyncSession, workspace_id) -> int:
    """The workspace's context cap, as the real quota service computes it."""
    from services.effective_quota_service import EffectiveQuotaService

    quotas = await EffectiveQuotaService(db_session).get_effective_quotas(workspace_id)
    return quotas["max_contexts"]


async def _fill_to_cap(db_session: AsyncSession, workspace_id, created_by: str, tag: str) -> int:
    """Top the workspace up to its cap with private contexts of ``created_by``.

    Returns the cap. The rows go with the fixture's teardown (it deletes every
    context of the workspace).
    """
    from sqlalchemy import func, select

    cap = await _cap(db_session, workspace_id)
    # One row per missing context: keep the plan's cap a size a test can fill.
    assert 0 < cap <= 200, cap
    existing = (
        await db_session.execute(
            select(func.count())
            .select_from(Context)
            .where(Context.workspace_id == workspace_id, Context.deleted_at.is_(None))
        )
    ).scalar_one()
    db_session.add_all(
        [
            Context(
                id=uuid4(),
                workspace_id=workspace_id,
                name=f"fill-{i}-{tag}",
                created_by=created_by,
                is_private=True,
            )
            for i in range(cap - existing)
        ]
    )
    await db_session.commit()
    return cap


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "extra", [{}, {"include_summary": True}, {"include_details": True}, {"include_stats": True}]
)
async def test_bound_workspace_is_listed_not_the_selected_one(two_workspaces, db_session, extra):
    s = two_workspaces
    payload = await _list(s, "wa", extra)

    assert payload["status"] == "success", payload
    # B is the user's selected workspace, but the request is bound to A.
    assert _ids(payload) == {str(s["a1"]), str(s["a2"])}
    assert payload["total"] == 2
    # #2891: count is what the caller sees in A (not the other member's private
    # context); limit / can_create describe A as a whole.
    assert payload["count"] == 2
    assert payload["limit"] == await _cap(db_session, s["wa"])
    assert payload["can_create"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_limit_and_can_create_come_from_the_bound_workspace(two_workspaces, db_session):
    """A is full (the other member's private contexts fill it); B, the selected
    workspace, has room. Bound to A the caller sees 2 contexts, cannot create a
    third and is told why; bound to B nothing about A shows."""
    from mcp_server.tools.context import _WORKSPACE_FULL_HINT

    s = two_workspaces
    cap = await _fill_to_cap(db_session, s["wa"], s["other_id"], s["tag"])

    in_a = await _list(s, "wa")
    in_b = await _list(s, "wb")

    assert _ids(in_a) == {str(s["a1"]), str(s["a2"])}
    assert (in_a["count"], in_a["total"], in_a["limit"], in_a["can_create"]) == (2, 2, cap, False)
    assert in_a["hint"] == _WORKSPACE_FULL_HINT
    assert (in_b["count"], in_b["total"], in_b["can_create"]) == (1, 1, True)
    assert in_b["limit"] == await _cap(db_session, s["wb"])
    assert "hint" not in in_b


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
async def test_a_full_workspace_the_user_does_not_belong_to_still_gets_no_hint(
    two_workspaces, db_session
):
    """#2891: the cap hint is never sent on the failed-lookup path, however full
    the workspace is."""
    s = two_workspaces
    await _fill_to_cap(db_session, s["wc"], s["outsider_id"], s["tag"])

    payload = await _list(s, "wc")

    assert payload["contexts"] == []
    assert payload["count"] == 0
    assert "hint" not in payload
    assert "limit" not in payload and "can_create" not in payload


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
    # #2891: count follows the same access rule in each workspace.
    assert in_a["count"] == 1
    assert in_b["count"] == 1


# ============================================================================
# #2891: count is what the caller can see; limit / can_create are the workspace's
# ============================================================================


@pytest_asyncio.fixture(loop_scope="session")
async def member_workspace(async_engine, db_session, monkeypatch):
    """One workspace: an owner with three private contexts and one shared one,
    a member whose access list holds the shared one only, and a member with an
    empty access list (sees nothing)."""
    import db.base as _db_base

    monkeypatch.setattr(
        _db_base,
        "async_session_factory",
        async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False),
    )

    tag = uuid4().hex[:8]
    owner_id, member_id, no_access_id = (
        f"vc-owner-{tag}",
        f"vc-member-{tag}",
        f"vc-none-{tag}",
    )
    ws = _workspace(tag, "vc", owner_id)
    db_session.add(ws)
    await db_session.flush()
    db_session.add_all(
        [_user(owner_id, ws.id), _user(member_id, ws.id), _user(no_access_id, ws.id)]
    )
    await db_session.flush()

    private = [
        Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"private-{i}-{tag}",
            created_by=owner_id,
            is_private=True,
        )
        for i in range(3)
    ]
    shared = Context(
        id=uuid4(), workspace_id=ws.id, name=f"shared-{tag}", created_by=owner_id, is_private=False
    )
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=owner_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=member_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[shared.id],
            ),
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=no_access_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[],
            ),
            *private,
            shared,
        ]
    )
    await db_session.commit()

    yield {
        "tag": tag,
        "ws": ws.id,
        "owner_id": owner_id,
        "member_id": member_id,
        "no_access_id": no_access_id,
        "shared": shared.id,
        "private": [ctx.id for ctx in private],
    }

    try:
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id == ws.id)
        )
        await db_session.execute(Context.__table__.delete().where(Context.workspace_id == ws.id))
        await db_session.execute(
            User.__table__.delete().where(User.user_id.in_([owner_id, member_id, no_access_id]))
        )
        await db_session.execute(Workspace.__table__.delete().where(Workspace.id == ws.id))
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


@pytest.mark.asyncio(loop_scope="session")
async def test_member_count_is_only_the_contexts_they_can_see(member_workspace, db_session):
    """The member sees 1 of 4 contexts and gets no figure for the other 3 while
    the workspace is under its cap; the owner gets everything they can see."""
    s = member_workspace
    cap = await _cap(db_session, s["ws"])

    member = await _list(s, "ws", user_key="member_id")
    owner = await _list(s, "ws", user_key="owner_id")

    assert _ids(member) == {str(s["shared"])}
    assert (member["count"], member["total"], member["limit"], member["can_create"]) == (
        1,
        1,
        cap,
        True,
    )
    assert "hint" not in member
    assert _ids(owner) == {str(s["shared"]), *map(str, s["private"])}
    assert (owner["count"], owner["total"], owner["limit"], owner["can_create"]) == (
        4,
        4,
        cap,
        True,
    )
    assert "hint" not in owner


@pytest.mark.asyncio(loop_scope="session")
async def test_member_of_a_full_workspace_gets_can_create_false_and_the_cap_hint(
    member_workspace, db_session
):
    from mcp_server.tools.context import _WORKSPACE_FULL_HINT

    s = member_workspace
    cap = await _fill_to_cap(db_session, s["ws"], s["owner_id"], s["tag"])

    member = await _list(s, "ws", user_key="member_id")

    assert (member["count"], member["total"], member["limit"], member["can_create"]) == (
        1,
        1,
        cap,
        False,
    )
    assert member["count"] < member["limit"]
    assert member["hint"] == _WORKSPACE_FULL_HINT


@pytest.mark.asyncio(loop_scope="session")
async def test_caller_who_sees_every_context_of_a_full_workspace_gets_no_hint(
    member_workspace, db_session
):
    s = member_workspace
    cap = await _fill_to_cap(db_session, s["ws"], s["owner_id"], s["tag"])

    owner = await _list(s, "ws", user_key="owner_id")

    assert owner["count"] == owner["limit"] == cap
    assert owner["can_create"] is False
    assert "hint" not in owner


@pytest.mark.asyncio(loop_scope="session")
async def test_name_contains_keeps_count_at_the_visible_number(member_workspace):
    s = member_workspace

    owner = await _list(s, "ws", {"name_contains": "private-"}, user_key="owner_id")
    member = await _list(s, "ws", {"name_contains": "private-"}, user_key="member_id")

    assert _ids(owner) == set(map(str, s["private"]))
    assert (owner["count"], owner["total"]) == (4, 3)
    # A filter aimed at contexts the member cannot see is a plain miss.
    assert member["contexts"] == []
    assert (member["count"], member["total"]) == (1, 0)
    assert "hint" not in member


@pytest.mark.asyncio(loop_scope="session")
async def test_member_who_sees_nothing_keeps_the_empty_hint(member_workspace):
    from mcp_server.tools.context import _EMPTY_CONTEXTS_HINT

    s = member_workspace
    payload = await _list(s, "ws", user_key="no_access_id")

    assert payload["contexts"] == []
    assert (payload["count"], payload["total"], payload["can_create"]) == (0, 0, True)
    assert payload["hint"] == _EMPTY_CONTEXTS_HINT


@pytest.mark.asyncio(loop_scope="session")
async def test_member_who_sees_nothing_in_a_full_workspace_gets_both_hints_cap_last(
    member_workspace, db_session
):
    from mcp_server.tools.context import _EMPTY_CONTEXTS_HINT, _WORKSPACE_FULL_HINT

    s = member_workspace
    cap = await _fill_to_cap(db_session, s["ws"], s["owner_id"], s["tag"])

    payload = await _list(s, "ws", user_key="no_access_id")

    assert (payload["count"], payload["limit"], payload["can_create"]) == (0, cap, False)
    assert payload["hint"] == f"{_EMPTY_CONTEXTS_HINT} {_WORKSPACE_FULL_HINT}"
