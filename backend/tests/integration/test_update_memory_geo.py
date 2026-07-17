"""Update/patch write paths × details.location (#1331, spec §8-4).

Pins against the real DB + MemoryService:
- the location contract fires on update/patch when details are supplied,
- the details replace-all contract drops location (round-trip pin, gate1
  regression note), and
- an update that does NOT touch details never 422s on a legacy row whose
  stored location predates the contract.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import SOURCE_TYPE_MANUAL, Memory
from models.schemas import PatchMemoryRequest, UpdateMemoryRequest
from services.memory_service import MemoryService


@pytest_asyncio.fixture(loop_scope="session")
async def geo_update_env(db_session):
    uid = f"e2e-geoupd-{uuid.uuid4().hex[:8]}"
    ws_id = uuid.uuid4()
    db_session.add(
        User(
            email=f"{uid}@example.test",
            user_id=uid,
            name="Geo Update E2E",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    await db_session.flush()
    db_session.add(
        Workspace(
            id=ws_id,
            name=f"ws-{uuid.uuid4().hex[:8]}",
            plan_name="free",
            owner_user_id=uid,
            daily_api_limit=500,
            weekly_api_limit=2500,
        )
    )
    db_session.add(WorkspaceMember(workspace_id=ws_id, user_id=uid, role=WorkspaceRole.OWNER))
    await db_session.flush()
    ctx = Context(id=uuid.uuid4(), workspace_id=ws_id, name="geo-upd", created_by=uid)
    db_session.add(ctx)
    await db_session.flush()

    def _mem(details):
        return Memory(
            id=uuid.uuid4(),
            user_id=uid,
            workspace_id=ws_id,
            context_id=ctx.id,
            summary="row under update",
            content="row under update",
            type="note",
            client="test",
            tags=[],
            source_type=SOURCE_TYPE_MANUAL,
            details=details,
        )

    located = _mem({"location": {"lat": 35.68, "lon": 139.76}, "note": "keep"})
    # Legacy shape: a numeric-looking string predating the contract (ORM
    # insert passes; the regex guard happens to populate it — the point is
    # the SERVICE contract must not 422 an update that never touches details).
    legacy = _mem({"location": "Tokyo office"})
    db_session.add_all([located, legacy])
    await db_session.flush()
    return {"uid": uid, "ws": ws_id, "ctx": ctx.id, "located": located.id, "legacy": legacy.id}


def _patched_side_effects():
    return (
        patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
    )


async def _cols(db_session, mem_id):
    return (
        await db_session.execute(
            text("SELECT location_lat, location_lon FROM memories WHERE id = :id"),
            {"id": mem_id},
        )
    ).one()


@pytest.mark.asyncio(loop_scope="session")
async def test_update_with_invalid_location_raises(geo_update_env, db_session):
    svc = MemoryService(db_session)
    with pytest.raises(ValueError, match="invalid details.location"):
        await svc.update_memory(
            UpdateMemoryRequest(
                memory_id=geo_update_env["located"],
                details={"location": {"lat": "35.6", "lon": 139.7}},
            ),
            user_id=geo_update_env["uid"],
            current_context_id=geo_update_env["ctx"],
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_update_details_replace_all_drops_location(geo_update_env, db_session):
    # The documented round-trip contract: resending details WITHOUT location
    # drops it — and the generated columns go NULL with it.
    svc = MemoryService(db_session)
    p1, p2 = _patched_side_effects()
    with p1, p2:
        await svc.update_memory(
            UpdateMemoryRequest(memory_id=geo_update_env["located"], details={"note": "only"}),
            user_id=geo_update_env["uid"],
            current_context_id=geo_update_env["ctx"],
        )
    row = await _cols(db_session, geo_update_env["located"])
    assert row.location_lat is None
    assert row.location_lon is None
    refreshed = await db_session.get(Memory, geo_update_env["located"])
    assert "location" not in refreshed.details


@pytest.mark.asyncio(loop_scope="session")
async def test_update_without_details_skips_location_validation(geo_update_env, db_session):
    # Importance-only update on a legacy-shaped row must not 422.
    svc = MemoryService(db_session)
    p1, p2 = _patched_side_effects()
    with p1, p2:
        response = await svc.update_memory(
            UpdateMemoryRequest(memory_id=geo_update_env["legacy"], importance=0.9),
            user_id=geo_update_env["uid"],
            current_context_id=geo_update_env["ctx"],
        )
    assert response.memory_id == geo_update_env["legacy"]


@pytest.mark.asyncio(loop_scope="session")
async def test_update_rejects_context_location(geo_update_env, db_session):
    svc = MemoryService(db_session)
    with pytest.raises(ValueError, match="context.location"):
        await svc.update_memory(
            UpdateMemoryRequest(
                memory_id=geo_update_env["located"],
                context={"location": {"lat": 1.0, "lon": 2.0}},
            ),
            user_id=geo_update_env["uid"],
            current_context_id=geo_update_env["ctx"],
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_patch_with_valid_location_populates_columns(geo_update_env, db_session):
    svc = MemoryService(db_session)
    p1, p2 = _patched_side_effects()
    with p1, p2:
        await svc.patch_memory(
            geo_update_env["legacy"],
            PatchMemoryRequest(details={"location": {"lat": -33.4489, "lon": -70.6693}}),
            geo_update_env["uid"],
        )
    row = await _cols(db_session, geo_update_env["legacy"])
    assert row.location_lat == pytest.approx(-33.4489)
    assert row.location_lon == pytest.approx(-70.6693)


@pytest.mark.asyncio(loop_scope="session")
async def test_patch_with_invalid_location_raises(geo_update_env, db_session):
    svc = MemoryService(db_session)
    with pytest.raises(ValueError, match="invalid details.location"):
        await svc.patch_memory(
            geo_update_env["legacy"],
            PatchMemoryRequest(details={"location": {"lat": 200, "lon": 0}}),
            geo_update_env["uid"],
        )
