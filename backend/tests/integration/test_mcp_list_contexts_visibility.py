"""#1600: ``list_contexts(name_contains=...)`` never widens visibility (CWE-639).

The unit tests (``tests/mcp_server/test_list_contexts_slim.py``) pin that the
filter runs on whatever the permission-scoped listing returned. This suite pins
the same property end to end against a real database and the real
``PermissionService``: a filter that textually matches a context the caller
must not see (another member's private context, a shared context outside the
caller's ``allowed_context_ids`` whitelist) still does not return it — in any
of the item shapes.

``count`` is deliberately NOT a visibility signal: it is the workspace-wide
quota count and already exceeded the caller-visible list before #1600.
"""

import json
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


@pytest_asyncio.fixture(loop_scope="session")
async def listing_scenario(async_engine, db_session, monkeypatch):
    """Workspace with an owner and a whitelisted member.

    Three contexts share the ``proj-`` name prefix so a single filter matches
    all of them textually:

    - ``shared``: shared, on the member's whitelist → visible to both
    - ``private``: owner's private context → owner only
    - ``unlisted``: shared but NOT on the member's whitelist → owner only
    """
    import db.base as _db_base

    # MCP handlers open their own session via get_db(); point it at the test engine.
    monkeypatch.setattr(
        _db_base,
        "async_session_factory",
        async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False),
    )

    tag = uuid4().hex[:8]
    owner_id, member_id = f"lc-owner-{tag}", f"lc-member-{tag}"
    ws = Workspace(
        id=uuid4(),
        name=f"ws-lc-{tag}",
        plan_name="pro",
        owner_user_id=owner_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    db_session.add(ws)
    await db_session.flush()

    users = [_user(owner_id, ws.id), _user(member_id, ws.id)]
    db_session.add_all(users)
    await db_session.flush()

    def _ctx(kind: str, **kwargs) -> Context:
        return Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"proj-{kind}-{tag}",
            created_by=owner_id,
            summary="要約" * 500,
            **kwargs,
        )

    shared = _ctx("shared", is_private=False, display_name=f"Alpha Team {tag}")
    private = _ctx("private", is_private=True)
    unlisted = _ctx("unlisted", is_private=False)
    db_session.add_all([shared, private, unlisted])
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=owner_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=member_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[shared.id],
            ),
        ]
    )
    await db_session.commit()

    yield {
        "tag": tag,
        "ws_id": ws.id,
        "owner_id": owner_id,
        "member_id": member_id,
        "shared": shared,
        "private": private,
        "unlisted": unlisted,
    }

    # Teardown — FK order: members → contexts → users → workspace.
    try:
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id == ws.id)
        )
        await db_session.execute(Context.__table__.delete().where(Context.workspace_id == ws.id))
        await db_session.execute(
            User.__table__.delete().where(User.user_id.in_([owner_id, member_id]))
        )
        await db_session.delete(ws)
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


async def _list(scenario, user_key: str, args: dict) -> dict:
    from mcp_server.tools.context import handle_list_contexts

    result = await handle_list_contexts(args, scenario[user_key], scenario["ws_id"])
    return json.loads(result[0].text)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "extra", [{}, {"include_summary": True}, {"include_details": True}, {"include_stats": True}]
)
async def test_name_filter_never_returns_a_context_the_caller_cannot_see(listing_scenario, extra):
    s = listing_scenario

    member = await _list(s, "member_id", {"name_contains": f"-{s['tag']}", **extra})
    owner = await _list(s, "owner_id", {"name_contains": f"-{s['tag']}", **extra})

    assert member["status"] == "success", member
    assert {c["id"] for c in member["contexts"]} == {str(s["shared"].id)}
    assert member["total"] == 1

    assert {c["id"] for c in owner["contexts"]} == {
        str(s["shared"].id),
        str(s["private"].id),
        str(s["unlisted"].id),
    }
    assert owner["total"] == 3

    # Quota meaning is unchanged: workspace-wide, identical for both callers.
    assert member["count"] == owner["count"] == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_filter_aimed_at_a_hidden_context_is_an_empty_success(listing_scenario):
    """Probing for a hidden context by name is indistinguishable from a miss."""
    s = listing_scenario

    hidden = await _list(s, "member_id", {"name_contains": s["private"].name})
    missing = await _list(s, "member_id", {"name_contains": f"no-such-context-{s['tag']}"})

    assert hidden == missing
    assert hidden["status"] == "success"
    assert hidden["contexts"] == [] and hidden["total"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_default_shape_and_display_name_match_on_real_rows(listing_scenario):
    s = listing_scenario

    payload = await _list(s, "member_id", {"name_contains": f"alpha team {s['tag']}"})

    assert [c["name"] for c in payload["contexts"]] == [s["shared"].name]
    assert set(payload["contexts"][0]) == {"id", "name", "is_private", "is_locked", "last_used_at"}
