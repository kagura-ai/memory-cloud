"""The admin user routes show how each account was admitted (#2803).

Over HTTP (the real query parsing) against Postgres, as a session admin and
as a service operator (``require_admin`` is overridden with each principal
it builds):

- ``GET /api/v1/admin/users/{id}``: ``admission`` = ``via``,
  ``invite_issuer``, ``invite_id`` and ``invited_by`` (``user_id``, ``email``)
  read live, for a user-minted invite; ``invited_by`` null for an
  admin-minted one, after the inviter's erasure, and for ``unknown``; the
  issuer is the recorded one, unchanged by a later promotion or demotion of
  the inviter, and a new inviter address shows on the next read;
- ``GET /api/v1/admin/users``: ``admitted_via`` on each row; the
  ``admitted_via`` and ``invited_by`` filters alone and combined with
  ``workspace_id``, ``plan``, ``role`` and ``search``, with the right
  ``total``; an invalid ``admitted_via`` is a 422;
- the invite's label appears in no response body, no log line from the
  sign-up through the admin calls, and no ``audit_logs`` row.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes import admin as admin_routes
from auth.dependencies import require_admin
from auth.roles import RoleManager
from auth.system_admin_mode import build_operator_principal
from config.settings import get_settings
from db.base import get_db
from models.auth import AuditLog, User, Workspace, WorkspaceMember
from models.beta_invite import BetaInvite
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from services.account_erasure_service import AccountErasureService
from services.signup_gate_service import SignupGateService
from utils.datetime import utcnow
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")

LABEL = "SENTINEL-LABEL-2803-admin-api"
PRINCIPALS = {
    "session": {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"},
    "service": build_operator_principal("ops@example.com"),
}


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


def _client(maker, principal: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api/v1")

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: principal
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession, maker, monkeypatch) -> AsyncIterator[SimpleNamespace]:
    """Accounts with every shape of record; one admitted through the real gate."""
    monkeypatch.setattr(get_settings(), "enable_beta_invites", True)
    tag = uuid4().hex[:8]
    made: list[str] = []

    async def person(handle: str, *, role: str = "user", **cols) -> str:
        user_id = cols.pop("user_id", f"{handle}-{tag}")
        db_session.add(
            User(
                email=f"{handle}-{tag}@admission.example",
                user_id=user_id,
                name=cols.pop("name", f"{handle} {tag}"),
                role=role,
                auth_method="oauth",
                auth_provider="google",
                **cols,
            )
        )
        await db_session.commit()
        made.append(user_id)
        return user_id

    inviter = await person("inviter")
    admin_inviter = await person("admininviter", role="admin")
    gone_inviter = await person("goneinviter")
    # A real invite minted by an admin: the admin-minted account points at
    # it, so a detail that reached the minter's CURRENT role (through the
    # invite row) would be caught when that role changes.
    admin_invite = BetaInvite(
        token_hash=sha256_hex(uuid4().hex + uuid4().hex),
        inviter_user_id=admin_inviter,
        expires_at=utcnow() + timedelta(days=7),
    )
    db_session.add(admin_invite)
    await db_session.commit()
    invite_ids = {"admin": admin_invite.id, "gone": uuid4()}
    admin_minted = await person(
        "adminminted",
        admitted_via="beta_invite",
        admitted_beta_invite_id=invite_ids["admin"],
        admitted_invite_issuer="admin",
    )
    orphan = await person(
        "orphan",
        admitted_via="beta_invite",
        admitted_beta_invite_id=invite_ids["gone"],
        admitted_invite_issuer="user",
        admitted_by_user_id=gone_inviter,
    )
    second = await person(
        "second",
        role="admin",
        admitted_via="beta_invite",
        admitted_beta_invite_id=uuid4(),
        admitted_invite_issuer="user",
        admitted_by_user_id=inviter,
    )
    allowlisted = await person("allowlisted", admitted_via="allowlist")
    # No name (an email sign-up may have none): the list still answers.
    plain = await person("plain", name=None)

    ws = Workspace(id=uuid4(), name=f"ws-{tag}", owner_user_id=inviter, plan_name="pro")
    db_session.add(ws)
    await db_session.flush()
    ws_id = ws.id
    for member in (inviter, second, allowlisted):
        db_session.add(
            WorkspaceMember(
                workspace_id=ws.id, user_id=member, role="owner" if member == inviter else "member"
            )
        )
    await db_session.commit()

    # The real sign-up: a user-minted invite with a label, through the gate.
    gate_svc = SignupGateService(db_session)
    config = await gate_svc.get_config()
    previous = (config.enabled, config.mode)
    await gate_svc.update_config(enabled=True, mode="manual")
    token = uuid4().hex + uuid4().hex
    invite = BetaInvite(
        token_hash=sha256_hex(token),
        inviter_user_id=inviter,
        expires_at=utcnow() + timedelta(days=7),
        label=LABEL,
    )
    db_session.add(invite)
    await db_session.commit()
    invite_ids["user"] = invite.id
    invitee = f"invitee-{tag}"
    email = f"invitee-{tag}@admission.example"
    made.append(invitee)

    async def _fake_get_db():
        async with maker() as s:
            yield s

    with structlog.testing.capture_logs() as events:
        async with maker() as s:
            answer = await SignupGateService(s).decide(
                provider="google",
                oauth_sub=invitee,
                email=email,
                username=email,
                beta_invite_token_hash=sha256_hex(token),
                email_verified=True,
            )
        with patch("db.base.get_db", new=_fake_get_db):
            await RoleManager(use_postgres=True).ensure_user(
                email=email,
                user_id=invitee,
                name=f"invitee {tag}",
                auth_provider="google",
                email_verified=True,
                admission=answer,
            )
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=invitee, role="member"))
    await db_session.commit()

    # The orphan's inviter is erased: only the link clears.
    async with maker() as s:
        target = await s.scalar(select(User).where(User.user_id == gone_inviter))
        await AccountErasureService(s)._delete_postgres(target)

    yield SimpleNamespace(
        tag=tag,
        inviter=inviter,
        admin_inviter=admin_inviter,
        invitee=invitee,
        admin_minted=admin_minted,
        orphan=orphan,
        second=second,
        allowlisted=allowlisted,
        plain=plain,
        workspace_id=ws.id,
        invite_ids=invite_ids,
        signup_events=events,
    )
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.execute(delete(Workspace).where(Workspace.id == ws_id))
    await db_session.execute(
        delete(SignupAllowlistEntry).where(SignupAllowlistEntry.subject_id == invitee)
    )
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id == invitee))
    await db_session.execute(delete(User).where(User.user_id.in_(made)))
    await db_session.commit()


@pytest.mark.parametrize("mode", list(PRINCIPALS))
class TestDetail:
    async def test_user_minted_invite_names_the_inviter_live(
        self, world, maker, db_session, mode
    ) -> None:
        def expected(email: str) -> dict:
            return {
                "via": "beta_invite",
                "invite_issuer": "user",
                "invite_id": str(world.invite_ids["user"]),
                "invited_by": {"user_id": world.inviter, "email": email},
            }

        new_email = f"renamed-{world.tag}@admission.example"
        async with _client(maker, PRINCIPALS[mode]) as client:
            first = await client.get(f"/api/v1/admin/users/{world.invitee}")
            # Live, not a copy: the inviter's new address shows on the next read.
            await db_session.execute(
                update(User).where(User.user_id == world.inviter).values(email=new_email)
            )
            await db_session.commit()
            second = await client.get(f"/api/v1/admin/users/{world.invitee}")

        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["admission"] == expected(f"inviter-{world.tag}@admission.example")
        assert second.json()["admission"] == expected(new_email)

    async def test_the_issuer_is_the_recorded_one_not_the_inviters_role_now(
        self, world, maker, db_session, mode
    ) -> None:
        # Promote the user inviter and demote the admin minter after the
        # sign-ups: the detail shows what was recorded at creation, never a
        # role read now (through the inviter's row or the invite's).
        await db_session.execute(
            update(User).where(User.user_id == world.inviter).values(role="admin")
        )
        await db_session.execute(
            update(User).where(User.user_id == world.admin_inviter).values(role="user")
        )
        await db_session.commit()
        async with _client(maker, PRINCIPALS[mode]) as client:
            invitee = (await client.get(f"/api/v1/admin/users/{world.invitee}")).json()
            minted = (await client.get(f"/api/v1/admin/users/{world.admin_minted}")).json()

        assert invitee["admission"] == {
            "via": "beta_invite",
            "invite_issuer": "user",
            "invite_id": str(world.invite_ids["user"]),
            "invited_by": {
                "user_id": world.inviter,
                "email": f"inviter-{world.tag}@admission.example",
            },
        }
        assert minted["admission"] == {
            "via": "beta_invite",
            "invite_issuer": "admin",
            "invite_id": str(world.invite_ids["admin"]),
            "invited_by": None,
        }

    async def test_admin_minted_invite_names_nobody(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = (await client.get(f"/api/v1/admin/users/{world.admin_minted}")).json()

        assert body["admission"] == {
            "via": "beta_invite",
            "invite_issuer": "admin",
            "invite_id": str(world.invite_ids["admin"]),
            "invited_by": None,
        }

    async def test_after_the_inviters_erasure(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = (await client.get(f"/api/v1/admin/users/{world.orphan}")).json()

        assert body["admission"] == {
            "via": "beta_invite",
            "invite_issuer": "user",
            "invite_id": str(world.invite_ids["gone"]),
            "invited_by": None,
        }

    async def test_unknown(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            body = (await client.get(f"/api/v1/admin/users/{world.plain}")).json()

        assert body["admission"] == {
            "via": "unknown",
            "invite_issuer": None,
            "invite_id": None,
            "invited_by": None,
        }


async def _list(client: httpx.AsyncClient, **params) -> tuple[int, list[str], int, dict]:
    response = await client.get("/api/v1/admin/users", params={"limit": 500, **params})
    if response.status_code != 200:
        return response.status_code, [], 0, {}
    body = response.json()
    vias = {u["id"]: u["admitted_via"] for u in body["users"]}
    return response.status_code, sorted(vias), body["total"], vias


@pytest.mark.parametrize("mode", list(PRINCIPALS))
class TestList:
    async def test_each_row_carries_admitted_via(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            _, _, _, vias = await _list(client, search=world.tag)

        assert vias == {
            world.inviter: "unknown",
            world.admin_inviter: "unknown",
            world.admin_minted: "beta_invite",
            world.orphan: "beta_invite",
            world.second: "beta_invite",
            world.allowlisted: "allowlist",
            world.plain: "unknown",
            world.invitee: "beta_invite",
        }

    async def test_admitted_via_alone_counts_the_channel(
        self, world, maker, db_session, mode
    ) -> None:
        expected = await db_session.scalar(
            select(func.count()).select_from(User).where(User.admitted_via == "beta_invite")
        )
        async with _client(maker, PRINCIPALS[mode]) as client:
            status, ids, total, vias = await _list(client, admitted_via="beta_invite")

        assert status == 200
        assert total == expected and len(ids) == expected
        assert set(vias.values()) == {"beta_invite"}

    async def test_invited_by_counts_the_inviters_people(self, world, maker, mode) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            _, ids, total, _ = await _list(client, invited_by=world.inviter)

        assert (ids, total) == (sorted([world.invitee, world.second]), 2)

    @pytest.mark.parametrize(
        ("extra", "expected"),
        [
            ({}, ["invitee", "second"]),
            ({"role": "admin"}, ["second"]),
            ({"plan": "pro"}, ["invitee", "second"]),
            ({"search": "second"}, ["second"]),
        ],
    )
    async def test_filters_combine_with_the_existing_ones(
        self, world, maker, mode, extra, expected
    ) -> None:
        params = {
            "admitted_via": "beta_invite",
            "invited_by": world.inviter,
            "workspace_id": str(world.workspace_id),
            **extra,
        }
        async with _client(maker, PRINCIPALS[mode]) as client:
            status, ids, total, _ = await _list(client, **params)

        names = sorted(getattr(world, n) for n in expected)
        assert (status, ids, total) == (200, names, len(names))

    async def test_admitted_via_with_workspace_counts_distinct_users(
        self, world, maker, mode
    ) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            _, ids, total, _ = await _list(
                client, admitted_via="allowlist", workspace_id=str(world.workspace_id)
            )

        assert (ids, total) == ([world.allowlisted], 1)

    @pytest.mark.parametrize(
        "params",
        [
            {"admitted_via": "invite"},
            {"admitted_via": "BETA_INVITE"},
            {"invited_by": ""},
            {"invited_by": "x" * 256},
        ],
    )
    async def test_invalid_filter_is_a_422(self, world, maker, mode, params) -> None:
        async with _client(maker, PRINCIPALS[mode]) as client:
            response = await client.get("/api/v1/admin/users", params=params)

        assert response.status_code == 422


async def test_the_label_appears_nowhere(world, maker, db_session, caplog, capsys) -> None:
    caplog.set_level(logging.DEBUG)
    bodies: list[str] = []
    with structlog.testing.capture_logs() as events:
        for principal in PRINCIPALS.values():
            async with _client(maker, principal) as client:
                for user_id in (world.invitee, world.second, world.orphan):
                    bodies.append((await client.get(f"/api/v1/admin/users/{user_id}")).text)
                for params in (
                    {"search": world.tag},
                    {"invited_by": world.inviter},
                    {"admitted_via": "beta_invite"},
                ):
                    bodies.append((await client.get("/api/v1/admin/users", params=params)).text)

    assert all(LABEL not in body for body in bodies)
    assert LABEL not in repr(world.signup_events)
    assert LABEL not in repr(events)
    assert LABEL not in caplog.text
    captured = capsys.readouterr()
    assert LABEL not in captured.out and LABEL not in captured.err
    leaked = await db_session.scalar(
        text("SELECT count(*) FROM audit_logs a WHERE a::text LIKE :p"), {"p": f"%{LABEL}%"}
    )
    assert leaked == 0
    # The filter values are not logged either.
    assert world.inviter not in repr([e for e in events if e.get("event") == "admin_list_users"])
