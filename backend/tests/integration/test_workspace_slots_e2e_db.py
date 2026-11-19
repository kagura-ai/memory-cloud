"""Workspace slots across the three parts of #2918, end to end on real rows.

The transition (``services.workspace_slots``) writes the suspension when a
billing delivery lowers the allowance, the gate (``services.workspace_slot_gate``)
refuses the suspended workspace on every surface, and the account routes
(``/api/v1/me/workspace-slots``) let the owner see and change it. Each part is
tested on its own elsewhere; this module drives one owner through all three:

* an owner with three workspaces whose paid plan ends (the ordered billing
  snapshot route): the default order applies at once, ``selection_required``;
* the suspended workspace answers ``SLOT-001`` on a workspace route, a context
  route and an MCP tool call, for the owner, a member and a workspace API key,
  while the owner's account page shows it and its export works;
* the owner swaps the selection through ``PUT .../selection``;
* an upgrade delivery resumes everything and the same calls answer again.

The same sequence with the mode ``off`` suspends nothing and runs no statement
on the slot tables. Everything goes through HTTP with a session cookie (a stub
session store behind the real session middleware) and a real API key; MCP
calls go through the MCP key authentication and the tool dispatcher.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

import api.main as api_main
from api.main import app
from config.settings import get_settings
from db.base import get_db
from models.auth import APIKey, Context, User, Workspace, WorkspaceMember
from models.workspace_billing_schemas import BILLING_V1_ADDON_KEYS
from services.workspace_slots import sweep_workspace_slots

from ._slot_world import SlotWorld, StatementRecorder, slot_mode

pytestmark = pytest.mark.asyncio(loop_scope="session")

SLOTS = "/api/v1/me/workspace-slots"
BILLING_TOKEN = "fixture-billing-2918-e2e"
_NOW = datetime.now(UTC).replace(microsecond=0)


class _Sessions:
    """The session store behind the real ``SessionMiddleware``: cookie -> user."""

    def __init__(self) -> None:
        self.by_id: dict[str, dict[str, str]] = {}

    def add(self, user_id: str) -> str:
        sid = f"e2e-{uuid4().hex}"
        self.by_id[sid] = {
            "user_id": user_id,
            "sub": user_id,
            "email": f"{user_id}@example.com",
            "name": user_id,
            "role": "user",
        }
        return sid

    def get_session(self, session_id: str, update_access: bool = True) -> dict | None:
        return self.by_id.get(session_id)


@dataclass
class E2E:
    world: SlotWorld
    client: httpx.AsyncClient
    owner: str
    member: str
    first: UUID  # Free, the owner's current workspace
    billed: UUID  # Max through the billing snapshot, then Free, then Max again
    shared: UUID  # Free with two extra seats: the member works here
    context: UUID  # a shared context of ``shared``
    cookie: dict[str, str]
    key: str  # the member's API key scoped to ``shared``


@pytest_asyncio.fixture(loop_scope="session")
async def e2e(slot_world: SlotWorld, monkeypatch) -> AsyncIterator[E2E]:
    from auth.api_keys import APIKeyManager

    world = slot_world
    owner = await world.user("e2e-owner")
    member = await world.user("e2e-member")
    first = await world.workspace(owner, current=True)
    billed = await world.workspace(owner)
    shared = await world.workspace(owner, members={member: "member"})
    context = uuid4()
    async with world.maker() as s:
        await s.execute(
            update(Workspace).where(Workspace.id == shared).values(addon_member_bonus=2)
        )
        s.add(
            Context(
                id=context,
                workspace_id=shared,
                name="e2e-shared",
                created_by=owner,
                is_private=False,
            )
        )
        await s.flush()
        await s.execute(
            update(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == shared, WorkspaceMember.user_id == member)
            .values(allowed_context_ids=[context])
        )
        await s.execute(
            update(User).where(User.user_id == member).values(current_workspace_id=shared)
        )
        key, _ = await APIKeyManager(s).create_key(
            name="e2e-member-key", user_id=member, workspace_id=shared
        )
        await s.commit()

    async def _db() -> AsyncIterator[AsyncSession]:
        async with world.maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _mcp_db() -> AsyncIterator[AsyncSession]:
        async with world.maker() as session:
            yield session

    sessions = _Sessions()
    monkeypatch.setattr(api_main, "_session_middleware_manager", sessions)
    monkeypatch.setattr(get_settings(), "billing_service_token", BILLING_TOKEN)
    app.dependency_overrides[get_db] = _db
    try:
        with (
            patch("db.base.get_db", new=_mcp_db),
            patch("mcp_server.tools._check_rate_limit", AsyncMock(return_value=(True, 0, 1000))),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                yield E2E(
                    world=world,
                    client=client,
                    owner=owner,
                    member=member,
                    first=first,
                    billed=billed,
                    shared=shared,
                    context=context,
                    cookie={owner: sessions.add(owner), member: sessions.add(member)},
                    key=key,
                )
    finally:
        app.dependency_overrides.pop(get_db, None)
        async with world.maker() as s:
            await s.execute(delete(APIKey).where(APIKey.user_id == member))
            await s.execute(delete(Context).where(Context.id == context))
            await s.commit()


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


async def _as(e: E2E, user_id: str, method: str, url: str, **kw: Any) -> httpx.Response:
    headers = {"Cookie": f"kagura_session={e.cookie[user_id]}"}
    return await e.client.request(method, url, headers=headers, **kw)


async def _with_key(e: E2E, method: str, url: str) -> httpx.Response:
    return await e.client.request(method, url, headers={"Authorization": f"Bearer {e.key}"})


async def _deliver(e: E2E, revision: int, plan: str) -> None:
    """One ordered billing snapshot for ``billed`` through the internal route."""
    window = {
        "valid_from": (_NOW - timedelta(days=1)).isoformat(),
        "valid_until": (_NOW + timedelta(days=30)).isoformat(),
        "evidence_ref": "paid-base-term",
    }
    body = {
        "revision": revision,
        "event_id": f"e2e-{e.billed}-{revision}",
        "source_ref": "workspace-snapshot",
        "contract_version": "workspace-billing-snapshot-v1",
        "max_access": window,
        "services": [],
        "plan": {"plan_name": plan, "addons": dict.fromkeys(BILLING_V1_ADDON_KEYS, 0)},
    }
    response = await e.client.put(
        f"/internal/workspaces/{e.billed}/billing-snapshot",
        json=body,
        headers={"Authorization": f"Bearer {BILLING_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "applied", response.json()


async def _mcp(e: E2E, caller: str) -> dict[str, Any]:
    """``get_context_info`` naming the shared context, as the MCP transport runs it."""
    from mcp_server.auth import authenticate_mcp_request
    from mcp_server.tools import execute_tool_call
    from mcp_server.tools._helpers import _mcp_key_workspace_scope

    if caller == "key":
        user_id, _, session_ws = await authenticate_mcp_request(
            authorization_header=f"Bearer {e.key}"
        )
    else:
        user_id = caller
        session_ws = e.first if caller == e.owner else e.shared
    token = _mcp_key_workspace_scope.set(session_ws if caller == "key" else None)
    try:
        result = await execute_tool_call(
            "get_context_info", {"context_id": str(e.context)}, user_id, session_ws
        )
    finally:
        _mcp_key_workspace_scope.reset(token)
    body = json.loads(result[0].text)
    return body if isinstance(body, dict) else {"body": body}


async def _probe(e: E2E) -> dict[str, tuple[int | str, str | None]]:
    """Each caller on a workspace route, a context route and MCP, for ``shared``."""
    out: dict[str, tuple[int | str, str | None]] = {}
    for label, call in (
        ("owner workspace", _as(e, e.owner, "GET", f"/api/v1/workspaces/{e.shared}")),
        ("member workspace", _as(e, e.member, "GET", f"/api/v1/workspaces/{e.shared}")),
        ("key workspace", _with_key(e, "GET", "/api/v1/workspace/stats")),
        ("owner context", _as(e, e.owner, "GET", f"/api/v1/contexts/{e.context}/tags")),
        ("member context", _as(e, e.member, "GET", f"/api/v1/contexts/{e.context}/tags")),
        ("key context", _with_key(e, "GET", f"/api/v1/contexts/{e.context}/tags")),
    ):
        response = await call
        body = response.json() if response.content else {}
        manage = (body.get("details") or {}).get("manage_url") if isinstance(body, dict) else None
        out[label] = (response.status_code, manage)
        if response.status_code == 403:
            assert body["error"] == "SLOT-001", (label, body)
            assert body["details"]["workspace_id"] == str(e.shared), (label, body)
        else:
            assert response.status_code == 200, (label, response.text)
    for label, caller in (("owner mcp", e.owner), ("member mcp", e.member), ("key mcp", "key")):
        body = await _mcp(e, caller)
        out[label] = (body.get("error") or "ok", body.get("manage_url"))
    return out


REFUSED_EVERYWHERE = {
    "owner workspace": 403,
    "member workspace": 403,
    "key workspace": 403,
    "owner context": 403,
    "member context": 403,
    "key context": 403,
    "owner mcp": "workspace_suspended",
    "member mcp": "workspace_suspended",
    "key mcp": "workspace_suspended",
}
SERVED_EVERYWHERE = dict.fromkeys(REFUSED_EVERYWHERE, 200) | {
    "owner mcp": "ok",
    "member mcp": "ok",
    "key mcp": "ok",
}


def _codes(probe: dict[str, tuple[int | str, str | None]]) -> dict[str, int | str]:
    return {label: code for label, (code, _manage) in probe.items()}


async def _statuses(e: E2E) -> dict[UUID, str]:
    body = (await _as(e, e.owner, "GET", SLOTS)).json()
    return {UUID(row["id"]): row["status"] for row in body["workspaces"]}


async def _listed(e: E2E, user_id: str) -> dict[UUID, bool]:
    rows = (await _as(e, user_id, "GET", "/api/v1/workspaces")).json()
    return {UUID(row["id"]): row["slot_suspended"] for row in rows}


async def _selection_required(e: E2E) -> bool:
    return (await _as(e, e.owner, "GET", "/api/v1/auth/me")).json()["user"][
        "workspace_selection_required"
    ]


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------


async def test_a_paid_plan_end_suspends_gates_is_managed_and_resumes(e2e: E2E) -> None:
    e = e2e
    with slot_mode("enforce"):
        await _deliver(e, 1, "promax")
        assert set((await _statuses(e)).values()) == {"active"}
        assert _codes(await _probe(e)) == SERVED_EVERYWHERE

        # The paid plan ends: allowance 1, the current workspace stays (default order).
        await _deliver(e, 2, "free")
        overview = (await _as(e, e.owner, "GET", SLOTS)).json()
        assert overview["selection_required"] is True
        assert (overview["active_count"], overview["active_slots"]) == (1, 1)
        statuses = await _statuses(e)
        assert statuses == {
            e.first: "active",
            e.billed: "slot_suspended",
            e.shared: "slot_suspended",
        }
        # One rule: the account overview and the workspace list (the gate's
        # SQL rule) agree on every owned workspace.
        assert await _listed(e, e.owner) == {
            wid: status == "slot_suspended" for wid, status in statuses.items()
        }
        assert await _listed(e, e.member) == {e.shared: True}
        assert await _selection_required(e) is True

        probe = await _probe(e)
        assert _codes(probe) == REFUSED_EVERYWHERE
        # The owner is sent to the account page; a member and a key's member are not.
        manage = get_settings().frontend_url.strip().rstrip("/") + "/profile/workspaces"
        assert probe["owner workspace"][1] == probe["owner context"][1] == manage
        assert probe["owner mcp"][1] == manage
        for label in ("member workspace", "member context", "key workspace", "key context"):
            assert probe[label][1] is None, label

        # The owner manages the suspended workspace: its contexts and export.
        listed = await _as(e, e.owner, "GET", f"{SLOTS}/workspaces/{e.shared}/contexts")
        assert listed.status_code == 200, listed.text
        assert [c["id"] for c in listed.json()["contexts"]] == [str(e.context)]
        export = await _as(e, e.owner, "GET", f"/api/v1/contexts/{e.context}/export")
        assert export.status_code == 200, export.text
        refused = await _as(e, e.member, "GET", f"/api/v1/contexts/{e.context}/export")
        assert (refused.status_code, refused.json()["error"]) == (403, "SLOT-001")

        # The sweep runs no gated primitive and changes nothing here.
        async with e.world.maker() as s:
            await sweep_workspace_slots(s)
        assert await _statuses(e) == statuses

        # The owner keeps ``shared`` instead: a swap within the allowance of 1.
        saved = await _as(
            e,
            e.owner,
            "PUT",
            f"{SLOTS}/selection",
            json={"workspace_ids": [str(e.shared)], "expected_version": overview["version"]},
        )
        assert saved.status_code == 200, saved.text
        assert {UUID(r["id"]): r["status"] for r in saved.json()["workspaces"]} == {
            e.first: "slot_suspended",
            e.billed: "slot_suspended",
            e.shared: "active",
        }
        assert saved.json()["selection_required"] is False
        assert await _selection_required(e) is False
        assert _codes(await _probe(e)) == SERVED_EVERYWHERE
        now_refused = await _as(e, e.owner, "GET", f"/api/v1/workspaces/{e.first}")
        assert (now_refused.status_code, now_refused.json()["error"]) == (403, "SLOT-001")

        # An upgrade delivery: Max again, every workspace resumes, plans untouched.
        await _deliver(e, 3, "promax")
        assert set((await _statuses(e)).values()) == {"active"}
        assert set((await _listed(e, e.owner)).values()) == {False}
        assert _codes(await _probe(e)) == SERVED_EVERYWHERE
        first = await _as(e, e.owner, "GET", f"/api/v1/workspaces/{e.first}")
        assert first.status_code == 200, first.text
        assert await e.world.plan_of(e.first) == ("free", "admin_grant")
        assert await e.world.plan_of(e.shared) == ("free", "admin_grant")
        assert await e.world.rows(e.owner) == {}


async def test_transfer_and_delete_meet_the_gate_and_the_transition(e2e: E2E) -> None:
    """The gate's opt-outs and the transition's hooks on the same routes."""
    e = e2e
    with slot_mode("enforce"):
        await _deliver(e, 1, "promax")
        await _deliver(e, 2, "free")
        transfer = {"target_user_id": e.member}
        # A suspended workspace cannot be given away: the gate answers first.
        suspended = await _as(
            e, e.owner, "POST", f"/api/v1/workspaces/{e.shared}/transfer-ownership", json=transfer
        )
        assert (suspended.status_code, suspended.json()["error"]) == (403, "SLOT-001")
        # An active one into an account with no free slot: the recipient check.
        await e.world.workspace(e.member)
        async with e.world.maker() as s:
            s.add(WorkspaceMember(workspace_id=e.first, user_id=e.member, role="member"))
            await s.commit()
        full = await _as(
            e, e.owner, "POST", f"/api/v1/workspaces/{e.first}/transfer-ownership", json=transfer
        )
        assert (full.status_code, full.json()["error"]) == (409, "SLOT-004"), full.text
        assert await e.world.owner_of(e.first) == e.owner

        # Deleting the active workspace (owner management) frees its slot: the
        # post-commit reconcile resumes one suspended workspace.
        deleted = await _as(e, e.owner, "DELETE", f"/api/v1/workspaces/{e.first}")
        assert deleted.status_code == 204, deleted.text
        statuses = await _statuses(e)
        assert sorted(statuses.values()) == ["active", "slot_suspended"], statuses
        assert await _listed(e, e.owner) == {
            wid: status == "slot_suspended" for wid, status in statuses.items()
        }
        member = await _as(e, e.member, "GET", f"/api/v1/workspaces/{e.shared}")
        assert member.status_code == (403 if statuses[e.shared] == "slot_suspended" else 200)


async def test_with_the_mode_off_nothing_is_suspended_and_no_slot_table_is_read(
    e2e: E2E, async_engine
) -> None:
    e = e2e
    with slot_mode("off"), StatementRecorder(async_engine) as recorder:
        await _deliver(e, 1, "promax")
        await _deliver(e, 2, "free")
        assert (await _as(e, e.owner, "GET", SLOTS)).status_code == 404
        assert (
            await _as(e, e.owner, "GET", f"{SLOTS}/workspaces/{e.shared}/contexts")
        ).status_code == 404
        assert _codes(await _probe(e)) == SERVED_EVERYWHERE
        assert set((await _listed(e, e.owner)).values()) == {False}
        assert await _selection_required(e) is False
        export = await _as(e, e.member, "GET", f"/api/v1/contexts/{e.context}/export")
        assert export.status_code == 200, export.text
        await _deliver(e, 3, "promax")
    assert recorder.statements, "the recorder saw nothing"
    assert recorder.touching_slot_tables() == []
    assert await e.world.rows(e.owner) == {}
    assert await e.world.state(e.owner) is None
