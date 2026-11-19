"""The publication-domain observation of a slot-suspended workspace (#2918).

The route reads through ``sync_authority`` like the other routes of an
installation: the owner check first, then the slot gate. The owner of a
suspended workspace gets ``SLOT-001``; anyone who could not see the
installation gets the same answer whether the workspace is suspended or not.
Real OIDC tokens and PostgreSQL (the fixtures of the installation tests).
"""

from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import delete

from config.settings import get_settings
from models.workspace_slots import WorkspaceSlotSuspension
from tests.integration import sync_installation_fixtures as fixtures
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store

ROLES = ("owner", "member", "admin", "viewer", "outsider")


async def test_the_observation_refuses_the_owner_of_a_suspended_workspace_only(
    store, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_public_usage_budget_enabled", True)
    monkeypatch.setattr(settings, "mcp_public_domains_routing_target", "edge.example.com")
    monkeypatch.setattr(settings, "mcp_public_domains_enabled", True)
    await fixtures.paid(store)
    approval = await fixtures.approve(store, fixtures.approval_body(application_id=str(uuid4())))
    activated = await fixtures.http(
        store, "POST", "sync-installations", body=fixtures.activation(approval)
    )
    assert activated.status_code == 201, activated.text
    path = f"sync-installations/{activated.json()['installation_id']}/publication-domains"

    async def answers() -> dict:
        return {role: await fixtures.http(store, "GET", path, role=role) for role in ROLES}

    with patch("services.workspace_slots.workspace_slot_mode", return_value="enforce"):
        active = await answers()
        async with store.factory() as db:
            db.add(
                WorkspaceSlotSuspension(
                    workspace_id=store.wid,
                    owner_user_id=store.users["owner"],
                    cause="billing_snapshot",
                    suspended_at=utcnow(),
                )
            )
            await db.commit()
        try:
            suspended = await answers()
        finally:
            async with store.factory() as db:
                await db.execute(
                    delete(WorkspaceSlotSuspension).where(
                        WorkspaceSlotSuspension.workspace_id == store.wid
                    )
                )
                await db.commit()
    assert active["owner"].status_code == 200, active["owner"].text
    assert suspended["owner"].status_code == 403, suspended["owner"].text
    assert suspended["owner"].json()["error"] == "SLOT-001"
    for role in ROLES[1:]:
        assert active[role].status_code == 404, (role, active[role].text)
        assert (suspended[role].status_code, suspended[role].content) == (
            active[role].status_code,
            active[role].content,
        ), role
