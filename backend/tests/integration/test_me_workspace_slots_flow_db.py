"""The workspace slot account routes through HTTP on real rows (#2918).

The service operations are pinned by ``test_workspace_slots_account_db.py``;
this module drives the same states through ``/api/v1/me/workspace-slots``
with one real session per request (committed or rolled back like
``db.base.get_db``) and an authentication dependency that reads the
``users`` row on that session first, as ``require_session_auth`` does. It
checks the JSON of every route, the statuses, the concurrency of two
saves, and the ``/auth/me`` and ``GET /workspaces`` flags.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.main import app
from auth.dependencies import require_session_auth
from db.base import get_db
from models.auth import User
from services.workspace_slots import SlotChange, owner_slot_lock, reconcile_workspace_slots

from ._slot_world import SlotWorld, slot_mode

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "/api/v1/me/workspace-slots"
Z_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


@asynccontextmanager
async def _http(world: SlotWorld, user_id: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def _db() -> AsyncGenerator[AsyncSession, None]:
        async with world.maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _session(db: AsyncSession = Depends(get_db)) -> dict:
        # Like require_session_auth: a plain read of the users row first.
        current = await db.scalar(select(User.current_workspace_id).where(User.user_id == user_id))
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "current_workspace_id": current,
        }

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_session_auth] = _session
    try:
        with patch(
            "api.routes.workspaces.get_current_user",
            AsyncMock(return_value={"user_id": user_id, "email": f"{user_id}@example.com"}),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_session_auth, None)


async def _reconcile(world: SlotWorld, owner: str, wid: UUID, plan: str, source: str) -> None:
    """``wid`` was ``plan`` / ``source`` before the event: reconcile it as a trigger does."""
    async with world.maker() as s:
        assert await owner_slot_lock(s, owner, wait=True)
        await reconcile_workspace_slots(
            s,
            owner,
            cause="billing_snapshot",
            allow_suspend=True,
            change=SlotChange(wid, plan, source),
        )
        await s.commit()


async def _issue_owner(world: SlotWorld) -> tuple[str, UUID, UUID, UUID]:
    """The issue's example: one Max workspace and two Free ones."""
    owner = await world.user("issue")
    personal = await world.workspace(owner, current=True)
    research = await world.workspace(owner, plan="promax", source="external_billing")
    side = await world.workspace(owner)
    return owner, personal, research, side


async def _term_end(world: SlotWorld, owner: str, research: UUID) -> None:
    await world.set_plan(research, "free", "external_billing")
    await _reconcile(world, owner, research, "promax", "external_billing")


def _statuses(body: dict[str, Any]) -> dict[str, str]:
    return {row["id"]: row["status"] for row in body["workspaces"]}


class TestIssueExampleThroughTheApi:
    async def test_term_end_selection_swap_and_upgrade(self, slot_world: SlotWorld) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        p, r, s = str(personal), str(research), str(side)
        with slot_mode("enforce"):
            async with _http(slot_world, owner) as client:
                before = (await client.get(BASE)).json()
                assert (before["owned_count"], before["active_count"]) == (3, 3)
                assert before["contracted_count"] == 1
                assert before["allowance"]["tier"] == "promax"
                assert before["if_paid_plans_end"]["cap"] == 1
                assert before["if_paid_plans_end"]["active"] == [p]
                assert sorted(before["if_paid_plans_end"]["suspended"]) == sorted([r, s])
                assert before["selection_required"] is False

                await _term_end(slot_world, owner, research)

                response = await client.get(BASE)
                assert response.status_code == 200, response.text
                after = response.json()
                assert set(after) == {
                    "allowance", "owned_count", "active_count", "active_slots",
                    "contracted_count", "selection_required", "selection", "version",
                    "if_paid_plans_end", "workspaces",
                }  # fmt: skip
                assert after["allowance"] == {
                    "cap": 1, "base": 1, "slot_bonus": 0, "tier_grant": 0, "tier": "free",
                    "next_tier": "pro", "cap_on_next_tier": 3,
                }  # fmt: skip
                assert (after["owned_count"], after["active_count"], after["active_slots"]) == (
                    3,
                    1,
                    1,
                )
                assert after["selection_required"] is True
                assert after["selection"] == {"workspace_ids": [], "saved_at": None}
                assert after["if_paid_plans_end"] is None
                # created_at ascending, then id; a row keeps its place.
                assert [row["id"] for row in after["workspaces"]] == [p, r, s]
                assert _statuses(after) == {p: "active", r: "slot_suspended", s: "slot_suspended"}
                for row in after["workspaces"]:
                    assert Z_TIMESTAMP.match(row["created_at"])
                    if row["status"] == "slot_suspended":
                        assert Z_TIMESTAMP.match(row["suspended_at"])
                    else:
                        assert row["suspended_at"] is None
                assert [row["is_current"] for row in after["workspaces"]] == [True, False, False]

                me = (await client.get("/api/v1/auth/me")).json()
                assert me["user"]["workspace_selection_required"] is True
                listed = {
                    row["id"]: row["slot_suspended"]
                    for row in (await client.get("/api/v1/workspaces")).json()
                }
                assert listed == {p: False, r: True, s: True}

                preview = await client.post(
                    f"{BASE}/preview", json={"workspace_ids": [r], "scenario": "current"}
                )
                assert preview.status_code == 200, preview.text
                assert preview.json() == {
                    "scenario": "current",
                    "cap": 1,
                    "active_slots": 1,
                    "active": [r],
                    "suspended": preview.json()["suspended"],
                    "unmet": [],
                    "changes": {"suspend": [p], "resume": [r]},
                }
                # The complete set after applying: Side stays suspended too.
                assert sorted(preview.json()["suspended"]) == sorted([p, s])
                assert (await client.get(BASE)).json() == after  # saved nothing

                saved = await client.put(
                    f"{BASE}/selection",
                    json={"workspace_ids": [r], "expected_version": after["version"]},
                )
                assert saved.status_code == 200, saved.text
                body = saved.json()
                assert _statuses(body) == {p: "slot_suspended", r: "active", s: "slot_suspended"}
                assert body["selection"]["workspace_ids"] == [r]
                assert Z_TIMESTAMP.match(body["selection"]["saved_at"])
                assert body["selection_required"] is False
                assert body["version"] > after["version"]
                assert (await client.get(BASE)).json() == body

                # Side is upgraded: the allowance covers all three again.
                await slot_world.set_plan(side, "promax", "external_billing")
                await _reconcile(slot_world, owner, side, "free", "admin_grant")

                upgraded = (await client.get(BASE)).json()
                assert {
                    row["id"]: (row["status"], row["plan_name"]) for row in upgraded["workspaces"]
                } == {
                    p: ("active", "free"),
                    r: ("active", "free"),
                    s: ("active", "promax"),
                }
                assert upgraded["selection_required"] is False
                listed = {
                    row["id"]: row["slot_suspended"]
                    for row in (await client.get("/api/v1/workspaces")).json()
                }
                assert listed == {p: False, r: False, s: False}

    async def test_resume_with_the_active_ids_in_any_order_is_a_swap(
        self, slot_world: SlotWorld
    ) -> None:
        """A resume sends ``[id, ...currently active ids]``, in any order."""
        owner, personal, research, side = await _issue_owner(slot_world)
        with slot_mode("enforce"):
            await _term_end(slot_world, owner, research)
            async with _http(slot_world, owner) as client:
                version = (await client.get(BASE)).json()["version"]
                saved = await client.put(
                    f"{BASE}/selection",
                    json={"workspace_ids": [str(side), str(personal)], "expected_version": version},
                )
        assert saved.status_code == 200, saved.text
        assert _statuses(saved.json()) == {
            str(personal): "slot_suspended",
            str(research): "slot_suspended",
            str(side): "active",
        }
        assert saved.json()["selection"]["workspace_ids"] == [str(side), str(personal)]


class TestIfPaidPlansEnd:
    async def test_null_without_a_billing_contract(self, slot_world: SlotWorld) -> None:
        owner = await slot_world.user("granted")
        await slot_world.workspace(owner, plan="pro", source="admin_grant", current=True)
        await slot_world.workspace(owner)
        with slot_mode("enforce"):
            async with _http(slot_world, owner) as client:
                body = (await client.get(BASE)).json()
        assert body["contracted_count"] == 0
        assert body["if_paid_plans_end"] is None

    async def test_populated_with_the_saved_selection(self, slot_world: SlotWorld) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        with slot_mode("enforce"):
            async with _http(slot_world, owner) as client:
                stored = await client.put(
                    f"{BASE}/selection", json={"workspace_ids": [str(side)], "expected_version": 0}
                )
                preview = await client.post(
                    f"{BASE}/preview",
                    json={"workspace_ids": [str(side)], "scenario": "paid_plans_end"},
                )
        assert stored.status_code == 200, stored.text
        assert stored.json()["active_count"] == 3  # nothing suspended: stored only
        assert stored.json()["if_paid_plans_end"]["cap"] == 1
        assert stored.json()["if_paid_plans_end"]["active"] == [str(side)]
        assert preview.json()["active"] == [str(side)]
        assert sorted(preview.json()["changes"]["suspend"]) == sorted(
            [str(personal), str(research)]
        )
        assert preview.json()["changes"]["resume"] == []


class TestRefusals:
    async def test_an_owner_of_nothing_gets_200_and_an_empty_list(
        self, slot_world: SlotWorld
    ) -> None:
        member = await slot_world.user("member")
        other = await slot_world.user("other")
        await slot_world.workspace(other, members={member: "admin"})
        with slot_mode("enforce"):
            async with _http(slot_world, member) as client:
                response = await client.get(BASE)
        assert response.status_code == 200
        body = response.json()
        assert body["workspaces"] == []
        assert (body["owned_count"], body["active_count"]) == (0, 0)
        assert body["active_slots"] == body["allowance"]["cap"] == 1
        assert body["if_paid_plans_end"] is None
        assert body["selection_required"] is False

    async def test_not_owned_and_unknown_ids_get_the_same_422(self, slot_world: SlotWorld) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        stranger = await slot_world.user("stranger")
        foreign = await slot_world.workspace(stranger)
        unknown = uuid4()
        bodies = []
        with slot_mode("enforce"):
            async with _http(slot_world, owner) as client:
                for wid in (foreign, unknown):
                    for method, path, extra in (
                        ("POST", f"{BASE}/preview", {"scenario": "current"}),
                        ("PUT", f"{BASE}/selection", {"expected_version": 0}),
                    ):
                        response = await client.request(
                            method, path, json={"workspace_ids": [str(wid)], **extra}
                        )
                        assert response.status_code == 422
                        body = response.json()
                        assert body["error"] == "VAL-001"
                        assert body["details"] == {
                            "field": "workspace_ids",
                            "invalid_ids": [str(wid)],
                        }
                        bodies.append(body)
                duplicated = await client.post(
                    f"{BASE}/preview",
                    json={"workspace_ids": [str(side), str(side)], "scenario": "current"},
                )
                version = (await client.get(BASE)).json()["version"]
        assert len({body["message"] for body in bodies}) == 1
        assert duplicated.status_code == 422
        assert duplicated.json()["details"]["invalid_ids"] == [str(side)]
        assert duplicated.json()["message"] == bodies[0]["message"]
        assert version == 0  # nothing was saved

    async def test_contracted_workspaces_filling_every_slot_is_slot_002(
        self, slot_world: SlotWorld
    ) -> None:
        owner = await slot_world.user("contracted")
        paid = await slot_world.workspace(owner, plan="basic", source="external_billing")
        free = await slot_world.workspace(owner)
        await slot_world.suspend_row(free, owner)
        with slot_mode("enforce"):
            async with _http(slot_world, owner) as client:
                response = await client.put(
                    f"{BASE}/selection", json={"workspace_ids": [str(free)], "expected_version": 0}
                )
                after = (await client.get(BASE)).json()
        assert response.status_code == 409
        assert response.json()["error"] == "SLOT-002"
        assert response.json()["details"] == {
            "cap": 1,
            "active_slots": 1,
            "active_count": 1,
            "contracted_count": 1,
            "next_tier": "pro",
            "cap_on_next_tier": 3,
            "unmet": [str(free)],
        }
        assert _statuses(after) == {str(paid): "active", str(free): "slot_suspended"}

    async def test_two_saves_at_once_give_one_200_and_one_slot_003(
        self, slot_world: SlotWorld
    ) -> None:
        owner, personal, research, side = await _issue_owner(slot_world)
        with slot_mode("enforce"):
            await _term_end(slot_world, owner, research)
            async with _http(slot_world, owner) as client:
                version = (await client.get(BASE)).json()["version"]
                responses = await asyncio.gather(
                    client.put(
                        f"{BASE}/selection",
                        json={"workspace_ids": [str(research)], "expected_version": version},
                    ),
                    client.put(
                        f"{BASE}/selection",
                        json={"workspace_ids": [str(side)], "expected_version": version},
                    ),
                )
                final = (await client.get(BASE)).json()
        codes = sorted(response.status_code for response in responses)
        assert codes == [200, 409]
        winner = next(r for r in responses if r.status_code == 200).json()
        loser = next(r for r in responses if r.status_code == 409).json()
        assert loser["error"] == "SLOT-003"
        assert loser["details"] == {
            "expected_version": version,
            "current_version": winner["version"],
        }
        assert final == winner
        assert [row["id"] for row in final["workspaces"] if row["status"] == "active"] == (
            winner["selection"]["workspace_ids"]
        )
