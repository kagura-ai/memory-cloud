"""A member's context access list at add time and by the owner key, on real rows (#2326).

* ``POST /api/v1/workspaces/{ws}/members`` takes an optional
  ``allowed_context_ids``: absent / null leaves the row's list unset, ``[]`` is
  no access, a list is stored once per id. An id that is not a live context of
  the workspace (another workspace's, a deleted one) is a 400 and no member row
  is written.
* ``PUT …/members/{user_id}/context-access`` accepts a workspace-owner API key
  (``authorize_workspace_management``) and records an ``AuditLog`` row with
  the change; an admin's key and a session member are refused, a session admin
  is not.

Every request runs on a session of its own on the test engine.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    tag: str
    owner: str
    admin: str
    member: str
    w: UUID  # the workspace members are added to
    x: UUID  # another workspace of the owner
    c1: UUID  # live contexts of W
    c2: UUID
    gone: UUID  # a deleted context of W
    x_ctx: UUID  # a live context of X


def _workspace(owner: str, tag: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"mac-{tag}-{uuid4().hex[:6]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, admin, member = (f"mac-{r}-{tag}" for r in ("own", "adm", "mem"))
    async with factory() as s:
        for u in (owner, admin, member):
            s.add(User(email=f"{u}@example.test", user_id=u, name=u, role="user"))
        await s.flush()
        w, x = _workspace(owner, "w"), _workspace(owner, "x")
        s.add_all([w, x])
        await s.flush()
        c1, c2, gone, x_ctx = (
            Context(id=uuid4(), workspace_id=ws, name=f"{n}-{tag}", created_by=owner, **kw)
            for ws, n, kw in (
                (w.id, "c1", {}),
                (w.id, "c2", {}),
                (w.id, "gone", {"deleted_at": utcnow()}),
                (x.id, "x", {}),
            )
        )
        s.add_all([c1, c2, gone, x_ctx])
        s.add_all(
            [
                WorkspaceMember(workspace_id=w.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=x.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=w.id, user_id=admin, role=WorkspaceRole.ADMIN),
                WorkspaceMember(workspace_id=w.id, user_id=member, role=WorkspaceRole.MEMBER),
            ]
        )
        await s.commit()
        ws_ids = [w.id, x.id]
        world = World(
            factory=factory,
            tag=tag,
            owner=owner,
            admin=admin,
            member=member,
            w=w.id,
            x=x.id,
            c1=c1.id,
            c2=c2.id,
            gone=gone.id,
            x_ctx=x_ctx.id,
        )
    try:
        yield world
    finally:
        async with factory() as s:
            for stmt in (
                delete(AuditLog).where(AuditLog.user_id.in_([owner, admin, member])),
                delete(Workspace).where(Workspace.id.in_(ws_ids)),
                delete(User).where(User.user_id.in_([owner, admin, member])),
            ):
                try:
                    await s.execute(stmt)
                    await s.commit()
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    await s.rollback()


def _session(user_id: str, ws: UUID) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "sub": user_id,
        "email": f"{user_id}@example.test",
        "current_workspace_id": ws,
    }


def _owner_key(user_id: str, ws: UUID) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "email": f"{user_id}@example.test",
        "current_workspace_id": ws,
        "api_key_workspace_id": ws,
        "api_key_prefix": "kagura_test",
    }


@contextlib.asynccontextmanager
async def _rest(w: World, principal: dict[str, Any]) -> AsyncIterator[Any]:
    import httpx

    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db

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

    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: principal
    app.dependency_overrides[get_db] = _db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        for s in opened:
            await s.close()


async def _member_row(w: World, user_id: str) -> WorkspaceMember | None:
    async with w.factory() as s:
        return (
            await s.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == w.w, WorkspaceMember.user_id == user_id
                )
            )
        ).scalar_one_or_none()


async def _audit(w: World, action: str, target: str) -> list[AuditLog]:
    async with w.factory() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars()
        return [r for r in rows if (r.user_metadata or {}).get("target") == target]


# ------------------------------------------------------------- POST …/members


async def test_session_owner_adds_a_member_with_a_list(world: World) -> None:
    w = world
    target = f"mac-t1-{w.tag}"
    async with _rest(w, _session(w.owner, w.w)) as c:
        r = await c.post(
            f"/api/v1/workspaces/{w.w}/members",
            json={
                "user_id": target,
                "role": "member",
                "allowed_context_ids": [str(w.c1), str(w.c2).upper(), str(w.c1)],
            },
        )
    assert r.status_code == 201, r.text
    assert r.json()["allowed_context_ids"] == [str(w.c1), str(w.c2)]
    row = await _member_row(w, target)
    assert row is not None
    assert row.allowed_context_ids == [w.c1, w.c2]
    # A session add is not audited (unchanged).
    assert await _audit(w, "workspace_member_added", target) == []


@pytest.mark.parametrize(
    ("payload", "stored"),
    [({}, None), ({"allowed_context_ids": None}, None), ({"allowed_context_ids": []}, [])],
    ids=["absent", "null", "empty"],
)
async def test_absent_null_and_empty(world: World, payload, stored) -> None:
    w = world
    target = f"mac-t2-{w.tag}-{len(payload)}-{stored is None}"
    async with _rest(w, _session(w.owner, w.w)) as c:
        r = await c.post(
            f"/api/v1/workspaces/{w.w}/members",
            json={"user_id": target, "role": "member", **payload},
        )
    assert r.status_code == 201, r.text
    assert r.json()["allowed_context_ids"] == stored
    row = await _member_row(w, target)
    assert row is not None
    assert row.allowed_context_ids == stored


@pytest.mark.parametrize("which", ["other_workspace", "deleted"])
async def test_a_context_outside_the_workspace_is_400_and_nothing_is_written(
    world: World, which: str
) -> None:
    w = world
    bad = w.x_ctx if which == "other_workspace" else w.gone
    target = f"mac-t3-{w.tag}-{which}"
    async with _rest(w, _session(w.owner, w.w)) as c:
        r = await c.post(
            f"/api/v1/workspaces/{w.w}/members",
            json={
                "user_id": target,
                "role": "member",
                "allowed_context_ids": [str(w.c1), str(bad)],
            },
        )
    assert r.status_code == 400, r.text
    assert r.json()["message"] == f"Invalid context IDs: ['{bad}']"
    assert await _member_row(w, target) is None


async def test_owner_key_adds_a_member_with_a_list_and_is_audited(world: World) -> None:
    w = world
    target = f"mac-t4-{w.tag}"
    async with _rest(w, _owner_key(w.owner, w.w)) as c:
        r = await c.post(
            f"/api/v1/workspaces/{w.w}/members",
            json={"user_id": target, "role": "viewer", "allowed_context_ids": [str(w.c2)]},
        )
    assert r.status_code == 201, r.text
    row = await _member_row(w, target)
    assert row is not None
    assert row.allowed_context_ids == [w.c2]
    (audit,) = await _audit(w, "workspace_member_added", target)
    assert audit.user_id == w.owner
    assert audit.user_metadata["role"] == "viewer"
    assert audit.user_metadata["context_count"] == 1
    assert audit.user_metadata["via"] == "api_key"


# -------------------------------------------------- PUT …/context-access


async def test_owner_key_sets_the_list_and_is_audited(world: World) -> None:
    w = world
    async with _rest(w, _owner_key(w.owner, w.w)) as c:
        r = await c.put(
            f"/api/v1/workspaces/{w.w}/members/{w.member}/context-access",
            json={"allowed_context_ids": [str(w.c1)]},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "user_id": w.member, "allowed_context_ids": [str(w.c1)]}
    row = await _member_row(w, w.member)
    assert row is not None
    assert row.allowed_context_ids == [w.c1]
    (audit,) = await _audit(w, "workspace_member_context_access_set", w.member)
    assert audit.user_id == w.owner
    assert audit.resource == f"workspace:{w.w}"
    assert audit.user_metadata["context_count"] == 1
    assert audit.user_metadata["via"] == "api_key"
    assert audit.user_metadata["key_prefix"] == "kagura_test"


async def test_owner_key_put_with_a_foreign_context_is_400_and_not_audited(world: World) -> None:
    w = world
    async with _rest(w, _owner_key(w.owner, w.w)) as c:
        r = await c.put(
            f"/api/v1/workspaces/{w.w}/members/{w.admin}/context-access",
            json={"allowed_context_ids": [str(w.x_ctx)]},
        )
    assert r.status_code == 400, r.text
    assert await _audit(w, "workspace_member_context_access_set", w.admin) == []


@pytest.mark.parametrize("caller", ["admin_key", "session_member"])
async def test_an_admin_key_and_a_session_member_are_refused(world: World, caller: str) -> None:
    w = world
    principal = _owner_key(w.admin, w.w) if caller == "admin_key" else _session(w.member, w.w)
    before = (await _member_row(w, w.member)).allowed_context_ids  # type: ignore[union-attr]
    async with _rest(w, principal) as c:
        r = await c.put(
            f"/api/v1/workspaces/{w.w}/members/{w.member}/context-access",
            json={"allowed_context_ids": []},
        )
    assert r.status_code == 403, r.text
    after = await _member_row(w, w.member)
    assert after is not None
    assert after.allowed_context_ids == before


async def test_a_session_admin_sets_the_list(world: World) -> None:
    w = world
    target = f"mac-t5-{w.tag}"
    async with w.factory() as s:
        s.add(WorkspaceMember(workspace_id=w.w, user_id=target, role=WorkspaceRole.VIEWER))
        await s.commit()
    async with _rest(w, _session(w.admin, w.w)) as c:
        r = await c.put(
            f"/api/v1/workspaces/{w.w}/members/{target}/context-access",
            json={"allowed_context_ids": []},
        )
    assert r.status_code == 200, r.text
    row = await _member_row(w, target)
    assert row is not None
    assert row.allowed_context_ids == []
    assert await _audit(w, "workspace_member_context_access_set", target) == []


def _error_body(r: Any) -> dict[str, Any]:
    """The error envelope without the per-request fields."""
    return {k: v for k, v in r.json().items() if k not in ("request_id", "timestamp")}


async def test_a_key_bound_to_another_workspace_gets_the_missing_workspace_404(
    world: World,
) -> None:
    w = world
    before = (await _member_row(w, w.member)).allowed_context_ids  # type: ignore[union-attr]
    audits = len(await _audit(w, "workspace_member_context_access_set", w.member))
    async with _rest(w, _owner_key(w.owner, w.x)) as c:
        r = await c.put(
            f"/api/v1/workspaces/{w.w}/members/{w.member}/context-access",
            json={"allowed_context_ids": []},
        )
        missing = await c.put(
            f"/api/v1/workspaces/{uuid4()}/members/{w.member}/context-access",
            json={"allowed_context_ids": []},
        )
    assert r.status_code == 404, r.text
    # Same answer as a workspace that does not exist: no hint that W does.
    assert missing.status_code == 404, missing.text
    assert _error_body(r) == _error_body(missing)
    assert str(w.w) not in r.text
    after = await _member_row(w, w.member)
    assert after is not None
    assert after.allowed_context_ids == before
    assert len(await _audit(w, "workspace_member_context_access_set", w.member)) == audits


async def test_an_oauth_token_is_refused(world: World) -> None:
    w = world
    principal = {
        "user_id": w.owner,
        "email": f"{w.owner}@example.test",
        "current_workspace_id": w.w,
        "oauth_scope": "memory:read memory:write",
    }
    before = (await _member_row(w, w.member)).allowed_context_ids  # type: ignore[union-attr]
    async with _rest(w, principal) as c:
        r = await c.put(
            f"/api/v1/workspaces/{w.w}/members/{w.member}/context-access",
            json={"allowed_context_ids": []},
        )
    assert r.status_code == 403, r.text
    assert "OAuth access tokens cannot manage workspace members" in r.json()["message"]
    after = await _member_row(w, w.member)
    assert after is not None
    assert after.allowed_context_ids == before


async def test_the_kill_switch_refuses_the_key_but_not_a_session(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.settings import get_settings

    w = world
    target = f"mac-t6-{w.tag}"
    async with w.factory() as s:
        s.add(WorkspaceMember(workspace_id=w.w, user_id=target, role=WorkspaceRole.MEMBER))
        await s.commit()
    monkeypatch.setattr(get_settings(), "enable_owner_key_member_management", False)
    url = f"/api/v1/workspaces/{w.w}/members/{target}/context-access"
    async with _rest(w, _owner_key(w.owner, w.w)) as c:
        r = await c.put(url, json={"allowed_context_ids": [str(w.c1)]})
    assert r.status_code == 403, r.text
    assert "disabled on this deployment" in r.json()["message"]
    row = await _member_row(w, target)
    assert row is not None
    assert row.allowed_context_ids is None
    async with _rest(w, _session(w.owner, w.w)) as c:
        r = await c.put(url, json={"allowed_context_ids": [str(w.c1)]})
    assert r.status_code == 200, r.text
    row = await _member_row(w, target)
    assert row is not None
    assert row.allowed_context_ids == [w.c1]
    assert await _audit(w, "workspace_member_context_access_set", target) == []


async def test_owner_key_put_for_a_non_member_is_404_and_leaves_no_audit_row(
    world: World,
) -> None:
    # The audit row is added before the service looks the member up; the
    # request's rollback must drop it with the 404.
    w = world
    target = f"mac-nobody-{w.tag}"
    async with _rest(w, _owner_key(w.owner, w.w)) as c:
        r = await c.put(
            f"/api/v1/workspaces/{w.w}/members/{target}/context-access",
            json={"allowed_context_ids": [str(w.c1)]},
        )
    assert r.status_code == 404, r.text
    assert await _member_row(w, target) is None
    assert await _audit(w, "workspace_member_context_access_set", target) == []


async def test_a_list_on_an_admin_is_stored_with_a_warning(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    import services.workspace_service as ws_mod

    w = world
    log = MagicMock()
    monkeypatch.setattr(ws_mod, "logger", log)
    target = f"mac-t7-{w.tag}"
    async with _rest(w, _owner_key(w.owner, w.w)) as c:
        r = await c.post(
            f"/api/v1/workspaces/{w.w}/members",
            json={"user_id": target, "role": "admin", "allowed_context_ids": [str(w.c1)]},
        )
    assert r.status_code == 201, r.text
    assert r.json()["allowed_context_ids"] == [str(w.c1)]
    row = await _member_row(w, target)
    assert row is not None
    assert row.allowed_context_ids == [w.c1]
    warnings = [str(call.args[0]) for call in log.warning.call_args_list]
    assert any("privileged role (ignored)" in m and target in m for m in warnings), warnings


async def test_member_to_viewer_role_change_reports_the_stored_list(world: World) -> None:
    # A member with no list becomes a viewer with [] (the service closes it);
    # the role response must say so, not report null ("every shared context").
    w = world
    target = f"mac-t8-{w.tag}"
    async with w.factory() as s:
        s.add(WorkspaceMember(workspace_id=w.w, user_id=target, role=WorkspaceRole.MEMBER))
        await s.commit()
    async with _rest(w, _session(w.owner, w.w)) as c:
        r = await c.put(f"/api/v1/workspaces/{w.w}/members/{target}", json={"role": "viewer"})
    assert r.status_code == 200, r.text
    row = await _member_row(w, target)
    assert row is not None
    assert row.allowed_context_ids == []
    assert r.json()["allowed_context_ids"] == []
