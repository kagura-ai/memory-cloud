"""A managing application sets the endpoint's public text, on real OIDC and PostgreSQL (#2995).

``POST /api/v1/workspaces/{wid}/sync-installations/{iid}/publication-text``
through the installation's registered browser client with the canonical
owner's ``integrations:manage`` token: who is admitted (the same admission as
the custom-domain commands), the endpoint it edits (only the binding's), the
owner API's limits and blank handling, the ``integration.updated`` record with
the acting client, the row lock, and a revoked installation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import Response
from sqlalchemy import delete, select, text, update

from config.settings import get_settings
from models.auth import WorkspaceMember
from models.operation_history import OperationHistory
from models.public_mcp import (
    PUBLIC_DESCRIPTION_MAX_CHARS,
    PUBLIC_USAGE_GUIDE_MAX_CHARS,
    PublicMcpEndpoint,
)
from models.workspace_slots import WorkspaceSlotSuspension
from tests.integration import sync_installation_fixtures as fixtures
from tests.integration.test_sync_publication_domain_commands_db import (
    NOT_FOUND,
    STALE,
    _request,
    binding,
    installed,
    token_for,
)
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store


@pytest_asyncio.fixture(loop_scope="session")
async def env(store, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_publication_text_operations_enabled", True)
    try:
        yield store
    finally:
        async with store.factory() as db:
            await db.execute(
                delete(OperationHistory).where(
                    OperationHistory.workspace_id.in_([store.wid, store.other])
                )
            )
            await db.commit()


def body(target: dict, **fields: Any) -> dict:
    return {"expected": binding(target), **fields}


async def set_text(
    store,
    target: dict,
    payload: dict,
    *,
    role: str = "owner",
    token: str | None = None,
    workspace: UUID | None = None,
    installation: str | None = None,
) -> Response:
    path = (
        f"/api/v1/workspaces/{workspace or store.wid}/sync-installations/"
        f"{installation or target['installation_id']}/publication-text"
    )
    return await _request(store, "POST", path, token or store.tokens[role], payload)


async def endpoint(store, target: dict) -> PublicMcpEndpoint:
    async with store.factory() as db:
        row = await db.get(PublicMcpEndpoint, UUID(target["endpoint_id"]))
        assert row is not None
        return row


async def history(store) -> list[OperationHistory]:
    async with store.factory() as db:
        return list(
            (
                await db.scalars(
                    select(OperationHistory)
                    .where(
                        OperationHistory.workspace_id == store.wid,
                        OperationHistory.operation == "integration.updated",
                    )
                    .order_by(OperationHistory.occurred_at)
                )
            ).all()
        )


def _text(row: PublicMcpEndpoint) -> tuple[str, str | None, str | None]:
    return row.public_name, row.public_description, row.public_usage_guide


# ----------------------------------------------------------------------------
# The owner's application sets the text; the record names the acting client
# ----------------------------------------------------------------------------


async def test_the_owner_sets_the_text_through_scoped_oidc(env):
    store = env
    target = await installed(store)
    before = await endpoint(store, target)
    response = await set_text(
        store,
        target,
        body(
            target,
            public_description="Example Shop product data.",
            public_usage_guide="Search by product name or model number.",
        ),
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    answer = response.json()
    assert answer["endpoint_id"] == target["endpoint_id"]
    assert answer["public_id"] == target["public_id"]
    assert answer["public_description"] == "Example Shop product data."
    assert answer["public_usage_guide"] == "Search by product name or model number."
    assert answer["changed_fields"] == ["description", "usage_guide"]
    assert answer["updated_at"] is not None
    after = await endpoint(store, target)
    assert after.public_name == before.public_name  # the owner's
    assert _text(after)[1:] == (answer["public_description"], answer["public_usage_guide"])
    assert after.updated_at is not None
    (event,) = await history(store)
    assert event.actor_user_id == store.users["owner"]
    assert event.subject_user_id == before.created_by
    assert (event.principal_kind, event.client_id, event.surface) == (
        "oauth",
        store.client_id,
        "api",
    )
    assert event.target_id == target["public_id"]
    assert event.context_id == UUID(target["context_id"])
    assert event.changed_fields == ["description", "usage_guide"]
    assert "Example Shop" not in str(event.__dict__)


async def test_omitted_keeps_blank_and_null_clear_and_no_change_records_nothing(env):
    store = env
    target = await installed(store)
    first = await set_text(store, target, body(target, public_usage_guide="Guide"))
    assert first.json()["changed_fields"] == ["usage_guide"]
    description = (await endpoint(store, target)).public_description
    assert description == "Approved public text"  # kept

    again = await set_text(store, target, body(target, public_usage_guide="Guide"))
    assert again.status_code == 200
    assert again.json()["changed_fields"] == []
    assert len(await history(store)) == 1

    cleared = await set_text(
        store, target, body(target, public_description="   ", public_usage_guide=None)
    )
    assert cleared.json()["changed_fields"] == ["description", "usage_guide"]
    row = await endpoint(store, target)
    assert (row.public_description, row.public_usage_guide) == (None, None)
    assert [event.changed_fields for event in await history(store)] == [
        ["usage_guide"],
        ["description", "usage_guide"],
    ]


async def test_the_owner_api_limits_hold_exactly(env):
    store = env
    target = await installed(store)
    at_limit = body(
        target,
        public_description="d" * PUBLIC_DESCRIPTION_MAX_CHARS,
        public_usage_guide="g" * PUBLIC_USAGE_GUIDE_MAX_CHARS,
    )
    assert (await set_text(store, target, at_limit)).status_code == 200
    for field, limit in (
        ("public_description", PUBLIC_DESCRIPTION_MAX_CHARS),
        ("public_usage_guide", PUBLIC_USAGE_GUIDE_MAX_CHARS),
    ):
        over = await set_text(store, target, body(target, **{field: "x" * (limit + 1)}))
        assert (over.status_code, over.json()) == (400, {"error": "invalid_request"}), field
    named = await set_text(store, target, body(target, public_name="Renamed"))
    assert (named.status_code, named.json()) == (400, {"error": "invalid_request"})
    row = await endpoint(store, target)
    assert row.public_name == "Approved catalog"
    assert len(row.public_description or "") == PUBLIC_DESCRIPTION_MAX_CHARS


# ----------------------------------------------------------------------------
# Authority and isolation
# ----------------------------------------------------------------------------


async def test_members_admins_viewers_outsiders_and_a_second_owner_get_one_404(env):
    store = env
    target = await installed(store)
    async with store.factory() as db:
        await db.execute(
            update(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == store.wid,
                WorkspaceMember.user_id == store.users["next"],
            )
            .values(role="owner")
        )
        await db.commit()
    before = _text(await endpoint(store, target))
    for role in ("member", "admin", "viewer", "outsider", "next"):
        response = await set_text(store, target, body(target, public_description="x"), role=role)
        assert (response.status_code, response.content) == (404, NOT_FOUND), role
    assert _text(await endpoint(store, target)) == before
    assert await history(store) == []


async def test_another_workspace_or_installation_is_404(env):
    store = env
    target = await installed(store)
    before = _text(await endpoint(store, target))
    for selection in (
        {"workspace": store.other},
        {"installation": str(uuid4())},
        {"workspace": store.other, "installation": str(uuid4())},
    ):
        response = await set_text(store, target, body(target, public_description="x"), **selection)
        assert (response.status_code, response.content) == (404, NOT_FOUND), selection
    assert _text(await endpoint(store, target)) == before


async def test_another_client_or_a_read_token_is_refused(env):
    store = env
    target = await installed(store)
    before = _text(await endpoint(store, target))
    foreign = await token_for(store, "owner", client_id=store.foreign_client)
    response = await set_text(store, target, body(target, public_description="x"), token=foreign)
    # The domain commands' answer for a client without a service registration.
    assert (response.status_code, response.json()) == (409, {"error": "RES-002"})
    reader = await token_for(store, "owner", scope="openid workspaces:read")
    response = await set_text(store, target, body(target, public_description="x"), token=reader)
    assert (response.status_code, response.json()) == (403, {"error": "insufficient_scope"})
    assert _text(await endpoint(store, target)) == before


async def test_a_revoked_installation_is_404_and_an_old_binding_stale(env):
    store = env
    target = await installed(store)
    before = _text(await endpoint(store, target))
    unlinked = await fixtures.http(
        store, "DELETE", f"sync-installations/{target['installation_id']}"
    )
    assert unlinked.status_code == 200, unlinked.text
    current = unlinked.json()
    assert current["state"] == "revoked"
    stale = await set_text(store, target, body(target, public_description="x"))
    assert (stale.status_code, stale.json()) == (409, STALE)
    revoked = await set_text(store, current, body(current, public_description="x"))
    assert (revoked.status_code, revoked.content) == (404, NOT_FOUND)
    assert _text(await endpoint(store, target)) == before
    assert await history(store) == []


@pytest.mark.parametrize(
    "field", ["application_id", "resource_id", "context_id", "endpoint_id", "generation"]
)
async def test_each_binding_field_is_compared(env, field):
    store = env
    target = await installed(store)
    wrong: dict[str, Any] = {
        "application_id": str(uuid4()),
        "resource_id": "sync-" + uuid4().hex,
        "context_id": str(uuid4()),
        "endpoint_id": str(uuid4()),
        "generation": target["generation"] + 1,
    }
    payload = {"expected": binding(target, **{field: wrong[field]}), "public_description": "x"}
    response = await set_text(store, target, payload)
    assert (response.status_code, response.json()) == (409, STALE), field
    assert await history(store) == []


async def test_a_republished_endpoint_is_404_and_never_edited(env):
    store = env
    target = await installed(store)
    async with store.factory() as db:
        old = await db.get(PublicMcpEndpoint, UUID(target["endpoint_id"]))
        assert old is not None
        old.revoked_at = utcnow()
        await db.flush()
        replacement = PublicMcpEndpoint(
            workspace_id=old.workspace_id,
            context_id=old.context_id,
            resource_pk=old.resource_pk,
            resource_id=old.resource_id,
            public_name=old.public_name,
            public_description="Owner text",
            created_by=old.created_by,
        )
        db.add(replacement)
        await db.commit()
        new_id = replacement.id
    for endpoint_id in (target["endpoint_id"], str(new_id)):
        payload = {
            "expected": binding(target, endpoint_id=endpoint_id),
            "public_description": "x",
        }
        response = await set_text(store, target, payload)
        assert (response.status_code, response.content) == (404, NOT_FOUND), endpoint_id
    async with store.factory() as db:
        row = await db.get(PublicMcpEndpoint, new_id)
        assert row is not None and row.public_description == "Owner text"


async def test_the_owner_of_a_suspended_workspace_gets_slot_001(env):
    store = env
    target = await installed(store)
    with patch("services.workspace_slots.workspace_slot_mode", return_value="enforce"):
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
            response = await set_text(store, target, body(target, public_description="x"))
        finally:
            async with store.factory() as db:
                await db.execute(
                    delete(WorkspaceSlotSuspension).where(
                        WorkspaceSlotSuspension.workspace_id == store.wid
                    )
                )
                await db.commit()
    assert (response.status_code, response.json()) == (403, {"error": "SLOT-001"})
    assert await history(store) == []


# ----------------------------------------------------------------------------
# The row lock and the transaction
# ----------------------------------------------------------------------------


async def test_a_held_endpoint_row_is_res_002_and_nothing_is_written(env):
    store = env
    target = await installed(store)
    before = _text(await endpoint(store, target))
    holder = store.factory()
    try:
        await holder.execute(
            text("SELECT id FROM public_mcp_endpoints WHERE id = :id FOR UPDATE"),
            {"id": UUID(target["endpoint_id"])},
        )
        response = await set_text(store, target, body(target, public_description="x"))
        assert (response.status_code, response.json()) == (409, {"error": "RES-002"})
    finally:
        await holder.rollback()
        await holder.close()
    assert _text(await endpoint(store, target)) == before
    assert await history(store) == []
    retried = await set_text(store, target, body(target, public_description="x"))
    assert retried.status_code == 200, retried.text


async def test_the_owner_patch_and_the_command_write_the_same_record_shape(env):
    """One function for both writers: the same changed-field names and actor."""
    from services.public_mcp_text import KEEP, apply_public_text

    store = env
    target = await installed(store)
    async with store.factory() as db:
        row = await db.get(PublicMcpEndpoint, UUID(target["endpoint_id"]))
        assert row is not None
        changed = apply_public_text(
            db, row, actor_user_id=store.users["owner"], name=KEEP, description="Owner text"
        )
        await db.commit()
    assert changed == ["description"]
    response = await set_text(store, target, body(target, public_description="App text"))
    assert response.json()["changed_fields"] == ["description"]
    owner_event, app_event = await history(store)
    assert owner_event.changed_fields == app_event.changed_fields == ["description"]
    assert owner_event.actor_user_id == app_event.actor_user_id == store.users["owner"]
    assert owner_event.target_id == app_event.target_id == target["public_id"]
    assert app_event.client_id == store.client_id
