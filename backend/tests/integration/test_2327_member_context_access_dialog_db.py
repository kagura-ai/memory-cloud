"""#2327: the context access dialog and context members outside the list.

``GET /workspaces/{id}/members/{user_id}/context-access`` serves the members
page's context access dialog: the member's role and list, and the contexts it
may be given — every live shared context of the workspace plus the member's
own private ones (``is_own_private``). Another account's private context and a
deleted context are never listed. Session admin+ or a workspace-owner API key.

``POST /contexts/{id}/members`` for a member or viewer whose context access
list does not reach the context answers 409 ``details.reason =
context_not_in_access_list`` (the row would grant nothing and be hidden from
the context member list) unless the request sends ``extend_access_list:
true``; then the context is added to the member's list in the same
transaction as the context member row. Only a workspace owner or admin may
extend a list.

The World is ``test_member_context_access_db``'s.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import ContextRole
from models.auth import Context, ContextMember, WorkspaceMember
from tests.integration import test_member_context_access_db as mca
from utils.datetime import utcnow

world = mca.world

pytestmark = pytest.mark.asyncio(loop_scope="session")


@contextlib.asynccontextmanager
async def _client(
    w: mca.World,
    caller: str,
    *,
    api_key: bool = False,
    key_workspace: str = "w",
    oauth: bool = False,
):
    """An HTTP client as ``caller``: a session principal, the caller's API key
    bound to workspace ``key_workspace``, or an OAuth token
    (``authorize_workspace_management`` tells them apart by ``sub`` /
    ``api_key_workspace_id`` / ``oauth_scope``)."""
    import httpx

    import api.routes.contexts as contexts_routes
    import api.routes.workspaces as workspaces_routes
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session, require_session_auth
    from db.base import get_db as orig_get_db

    opened: list[AsyncSession] = []

    async def _db() -> AsyncIterator[AsyncSession]:
        s = w.factory()
        opened.append(s)
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise

    uid = w.u[caller]
    principal: dict[str, Any] = {
        "user_id": uid,
        "email": f"{uid}@example.test",
        "current_workspace_id": w.ws["w"],
    }
    if api_key:
        principal["api_key_workspace_id"] = w.ws[key_workspace]
        principal["api_key_prefix"] = "kgr_test"
    elif oauth:
        principal["oauth_scope"] = "memory:read memory:write"
    else:
        principal["sub"] = uid
    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: principal
    app.dependency_overrides[require_session_auth] = lambda: principal
    for fn in {orig_get_db, contexts_routes.get_db, workspaces_routes.get_db}:
        app.dependency_overrides[fn] = _db
    try:
        with (
            patch("api.routes.contexts.get_current_user", AsyncMock(return_value=principal)),
            patch("api.routes.workspaces.get_current_user", AsyncMock(return_value=principal)),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as client:
                yield client
    finally:
        app.dependency_overrides.clear()
        for s in opened:
            await s.close()


def _access_url(w: mca.World, target_user_id: str) -> str:
    return f"/api/v1/workspaces/{w.ws['w']}/members/{target_user_id}/context-access"


async def _member(w: mca.World, caller: str) -> WorkspaceMember:
    w.session.expunge_all()
    return (
        await w.session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == w.ws["w"], WorkspaceMember.user_id == w.u[caller]
            )
        )
    ).scalar_one()


async def _context_member(w: mca.World, caller: str, ctx: str) -> ContextMember | None:
    w.session.expunge_all()
    return (
        await w.session.execute(
            select(ContextMember).where(
                ContextMember.context_id == w.ctx[ctx], ContextMember.user_id == w.u[caller]
            )
        )
    ).scalar_one_or_none()


def _ids(w: mca.World, keys: list[str] | None) -> list[UUID] | None:
    return None if keys is None else [w.ctx[k] for k in keys]


# ------------------------------------------------- GET …/context-access


@pytest.mark.parametrize("target", ["m_null_creator", "v_null", "mA", "owner", "admin", "linked_m"])
async def test_dialog_lists_shared_and_the_members_own_private_contexts(world, target) -> None:
    w: mca.World = world
    s = w.session
    # Deleted contexts are never offered, shared or the member's own.
    for private in (False, True):
        s.add(
            Context(
                id=uuid4(),
                workspace_id=w.ws["w"],
                name=f"gone-{int(private)}-{uuid4().hex[:6]}",
                created_by=w.u[target],
                is_private=private,
                deleted_at=utcnow(),
            )
        )
    await s.commit()

    async with _client(w, "owner") as c:
        r = await c.get(_access_url(w, w.u[target]))
    assert r.status_code == 200, r.text
    body = r.json()
    role, allowed = mca.CALLERS[target]
    assert body["user_id"] == w.u[target]
    assert body["role"] == role.value
    want_allowed = _ids(w, allowed)
    assert body["allowed_context_ids"] == (
        None if want_allowed is None else [str(i) for i in want_allowed]
    )

    by_id = {str(w.ctx[k]): k for k in mca.CONTEXTS}
    listed = [by_id.get(row["id"], row["id"]) for row in body["contexts"]]
    # Shared contexts, plus the private ones the member owns: its own, or
    # one of an account linked to it (#1784; linked_m is linked to the owner,
    # so it is offered pO).
    expected = {
        key
        for key, (private, _) in mca.CONTEXTS.items()
        if not private or mca._owns_privately(target, key)
    }
    assert set(listed) == expected, listed
    # Ordered by name (the World names a context after its key).
    assert [row["name"] for row in body["contexts"]] == sorted(
        row["name"] for row in body["contexts"]
    )
    for row in body["contexts"]:
        private, _ = mca.CONTEXTS[by_id[row["id"]]]
        assert row["is_private"] is private
        assert row["is_own_private"] is mca._owns_privately(target, by_id[row["id"]])
        assert set(row) == {"id", "name", "display_name", "summary", "is_private", "is_own_private"}


@pytest.mark.parametrize("missing", ["outsider", "nobody"])
async def test_dialog_read_is_404_for_a_user_outside_the_workspace(world, missing) -> None:
    w: mca.World = world
    user_id = w.u["outsider"] if missing == "outsider" else f"nobody-{uuid4().hex[:8]}"
    async with _client(w, "owner") as c:
        r = await c.get(_access_url(w, user_id))
    assert r.status_code == 404, r.text


@pytest.mark.parametrize(
    ("caller", "api_key", "status"),
    [
        ("mA", False, 403),
        ("v_null", False, 403),
        ("admin", False, 200),
        ("owner", False, 200),
        ("owner", True, 200),
        ("admin", True, 403),
    ],
)
async def test_dialog_read_needs_admin_or_an_owner_key(world, caller, api_key, status) -> None:
    w: mca.World = world
    async with _client(w, caller, api_key=api_key) as c:
        r = await c.get(_access_url(w, w.u["m_null_creator"]))
    assert r.status_code == status, r.text


async def test_dialog_read_refuses_an_oauth_token(world) -> None:
    w: mca.World = world
    async with _client(w, "owner", oauth=True) as c:
        r = await c.get(_access_url(w, w.u["m_null_creator"]))
    assert r.status_code == 403, r.text


async def test_dialog_read_with_a_key_bound_elsewhere_is_a_uniform_404(world) -> None:
    """The workspace owner's key bound to another workspace learns nothing:
    the same 404 for this workspace as for one that does not exist."""
    w: mca.World = world
    member = w.u["m_null_creator"]
    async with _client(w, "owner", api_key=True, key_workspace="x") as c:
        here = await c.get(_access_url(w, member))
        missing = await c.get(f"/api/v1/workspaces/{uuid4()}/members/{member}/context-access")
    assert here.status_code == 404, here.text
    assert missing.status_code == 404, missing.text
    assert here.json() == missing.json()


# --------------------------------------------- POST /contexts/{id}/members


@pytest.mark.parametrize(
    ("target", "after"),
    [
        ("m_null", ["B"]),
        ("m_empty", ["B"]),
        ("mA", ["A", "B"]),
        ("vA", ["A", "B"]),
    ],
)
async def test_adding_a_member_outside_its_list_needs_the_flag(world, target, after) -> None:
    w: mca.World = world
    before = mca.CALLERS[target][1]
    url = f"/api/v1/contexts/{w.ctx['B']}/members"
    async with _client(w, "owner") as c:
        refused = await c.post(url, json={"user_id": w.u[target], "role": "editor"})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["error"] == "RES-002"
    assert "extend_access_list" in body["message"]
    # The flag is an owner's or admin's (a context owner below admin gets the
    # same 409), so the message says who may send it.
    assert "owner or admin" in body["message"]
    assert body["details"] == {
        "reason": "context_not_in_access_list",
        "user_id": w.u[target],
        "context_id": str(w.ctx["B"]),
    }
    # Nothing was written.
    unchanged = await _member(w, target)
    assert unchanged.allowed_context_ids == _ids(w, before)
    updated_before = unchanged.updated_at
    assert await _context_member(w, target, "B") is None

    with structlog.testing.capture_logs() as logs:
        async with _client(w, "owner") as c:
            added = await c.post(
                url, json={"user_id": w.u[target], "role": "editor", "extend_access_list": True}
            )
            listed = await c.get(url)
    assert added.status_code == 201, added.text
    assert added.json()["access_list_extended"] is True
    assert added.json()["user_id"] == w.u[target]
    extended = await _member(w, target)
    assert extended.allowed_context_ids == _ids(w, after)
    assert extended.updated_at is not None
    assert updated_before is None or extended.updated_at > updated_before
    assert [
        (e["user_id"], e["context_id"], e["actor_user_id"])
        for e in logs
        if e["event"] == "member_context_access_extended"
    ] == [(w.u[target], str(w.ctx["B"]), w.u["owner"])]
    row = await _context_member(w, target, "B")
    assert row is not None and row.role == ContextRole.EDITOR
    # The row now grants the context, so the context member list shows it.
    assert listed.status_code == 200, listed.text
    assert w.u[target] in {m["user_id"] for m in listed.json()}


@pytest.mark.parametrize(
    ("caller", "target", "ctx", "flag"),
    [
        # A viewer with no list reaches every shared context.
        ("owner", "v_null", "A", False),
        # The flag changes nothing for a member that already reaches it.
        ("owner", "vA", "A", True),
        # Owners and admins reach every context; their list is ignored.
        ("owner", "admin", "A", False),
        ("owner", "admin", "B", True),
        ("admin", "owner", "B", True),
    ],
)
async def test_a_member_that_reaches_the_context_keeps_its_list(
    world, caller, target, ctx, flag
) -> None:
    w: mca.World = world
    before = (await _member(w, target)).allowed_context_ids
    payload: dict[str, Any] = {"user_id": w.u[target], "role": "editor"}
    if flag:
        payload["extend_access_list"] = True
    async with _client(w, caller) as c:
        r = await c.post(f"/api/v1/contexts/{w.ctx[ctx]}/members", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["access_list_extended"] is False
    assert (await _member(w, target)).allowed_context_ids == before
    assert await _context_member(w, target, ctx) is not None


async def test_only_a_workspace_admin_may_extend_a_list(world) -> None:
    """A context owner below workspace admin manages the context's members,
    not anyone's workspace context access list."""
    w: mca.World = world
    s = w.session
    s.add(ContextMember(context_id=w.ctx["B"], user_id=w.u["mA"], role=ContextRole.OWNER))
    await s.execute(
        WorkspaceMember.__table__.update()
        .where(WorkspaceMember.workspace_id == w.ws["w"], WorkspaceMember.user_id == w.u["mA"])
        .values(allowed_context_ids=[w.ctx["A"], w.ctx["B"]])
    )
    await s.commit()
    url = f"/api/v1/contexts/{w.ctx['B']}/members"
    async with _client(w, "mA") as c:
        refused = await c.post(url, json={"user_id": w.u["m_empty"], "role": "editor"})
        forbidden = await c.post(
            url, json={"user_id": w.u["m_empty"], "role": "editor", "extend_access_list": True}
        )
    assert refused.status_code == 409, refused.text
    assert refused.json()["details"]["reason"] == "context_not_in_access_list"
    assert "owner or admin" in refused.json()["message"]
    assert forbidden.status_code == 403, forbidden.text
    # The caller already manages this context, so the refusal can say why;
    # the web UI shows this message after its confirm dialog.
    assert "owner or admin" in forbidden.json()["message"]
    assert (await _member(w, "m_empty")).allowed_context_ids == []
    assert await _context_member(w, "m_empty", "B") is None


async def _context_member_rows(w: mca.World, caller: str, ctx: str) -> list[ContextMember]:
    w.session.expunge_all()
    return list(
        (
            await w.session.execute(
                select(ContextMember).where(
                    ContextMember.context_id == w.ctx[ctx], ContextMember.user_id == w.u[caller]
                )
            )
        )
        .scalars()
        .all()
    )


async def test_a_hidden_context_member_row_is_repaired_through_the_add(world) -> None:
    """m_null (a member with no list) holds an editor row in A that grants
    nothing and is hidden from the context member list. The add treats it as
    a new add: 409 without the flag; with it the list is extended and the
    existing row kept at the requested role."""
    w: mca.World = world
    url = f"/api/v1/contexts/{w.ctx['A']}/members"
    assert [r.role for r in await _context_member_rows(w, "m_null", "A")] == [ContextRole.EDITOR]
    async with _client(w, "owner") as c:
        hidden = await c.get(url)
        refused = await c.post(url, json={"user_id": w.u["m_null"], "role": "viewer"})
    assert hidden.status_code == 200, hidden.text
    assert w.u["m_null"] not in {m["user_id"] for m in hidden.json()}
    assert refused.status_code == 409, refused.text
    assert refused.json()["details"] == {
        "reason": "context_not_in_access_list",
        "user_id": w.u["m_null"],
        "context_id": str(w.ctx["A"]),
    }
    assert (await _member(w, "m_null")).allowed_context_ids is None
    assert [r.role for r in await _context_member_rows(w, "m_null", "A")] == [ContextRole.EDITOR]

    async with _client(w, "owner") as c:
        added = await c.post(
            url, json={"user_id": w.u["m_null"], "role": "viewer", "extend_access_list": True}
        )
        listed = await c.get(url)
    assert added.status_code == 201, added.text
    assert added.json()["access_list_extended"] is True
    assert added.json()["role"] == "viewer"
    assert (await _member(w, "m_null")).allowed_context_ids == [w.ctx["A"]]
    # The same row, now at the requested role; no second row.
    assert [r.role for r in await _context_member_rows(w, "m_null", "A")] == [ContextRole.VIEWER]
    assert listed.status_code == 200, listed.text
    assert {m["user_id"]: m["role"] for m in listed.json()}[w.u["m_null"]] == "viewer"


@pytest.mark.parametrize("flag", [False, True])
async def test_a_row_that_grants_the_context_is_a_duplicate(world, flag) -> None:
    """mA's list reaches A and it holds an editor row there: a real duplicate."""
    w: mca.World = world
    payload: dict[str, Any] = {"user_id": w.u["mA"], "role": "viewer"}
    if flag:
        payload["extend_access_list"] = True
    async with _client(w, "owner") as c:
        r = await c.post(f"/api/v1/contexts/{w.ctx['A']}/members", json=payload)
    assert r.status_code == 400, r.text
    assert "already a context member" in r.text
    assert (await _member(w, "mA")).allowed_context_ids == [w.ctx["A"]]
    assert [r.role for r in await _context_member_rows(w, "mA", "A")] == [ContextRole.EDITOR]


async def test_the_list_and_the_row_are_one_commit(world) -> None:
    """A failure writing the context member row leaves the list as it was."""
    w: mca.World = world

    def _fail(*_args: Any) -> None:
        raise RuntimeError("context member insert failed")

    event.listen(ContextMember, "before_insert", _fail)
    try:
        async with _client(w, "owner") as c:
            with pytest.raises(RuntimeError, match="context member insert failed"):
                await c.post(
                    f"/api/v1/contexts/{w.ctx['B']}/members",
                    json={"user_id": w.u["mA"], "role": "editor", "extend_access_list": True},
                )
    finally:
        event.remove(ContextMember, "before_insert", _fail)
    assert (await _member(w, "mA")).allowed_context_ids == [w.ctx["A"]]
    assert await _context_member(w, "mA", "B") is None
