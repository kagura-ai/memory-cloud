"""Real-PostgreSQL behavior of the WHERE-axis nearby query (#1331).

The mock-db suite (tests/mcp_server/test_recall_nearby.py) pins the SQL
shape; this pins the actual semantics against the generated columns:
distance ordering, radius cutoff, tombstone exclusion, antimeridian
retrieval, and the #1299 binding filter hook.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import WorkspaceRole
from models.agent import Agent
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import SOURCE_TYPE_MANUAL, Memory
from services.agent_binding_service import AgentBindingService
from services.geo_memory import query_nearby_memories

# Tokyo Station-ish anchor.
LAT, LON = 35.6812, 139.7671


@pytest.fixture(autouse=True)
def _clean_scope():
    set_agent_scope(None)
    yield
    set_agent_scope(None)


@pytest_asyncio.fixture(loop_scope="session")
async def geo_env(db_session):
    uid = f"e2e-geo-{uuid.uuid4().hex[:8]}"
    ws_id = uuid.uuid4()
    db_session.add(
        User(
            email=f"{uid}@example.test",
            user_id=uid,
            name="Geo E2E",
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
    ctx = Context(id=uuid.uuid4(), workspace_id=ws_id, name="geo", created_by=uid)
    db_session.add(ctx)
    await db_session.flush()

    def _mem(summary, lat, lon, *, mtype="note", deleted=False):
        from utils.datetime import utcnow

        return Memory(
            id=uuid.uuid4(),
            user_id=uid,
            workspace_id=ws_id,
            context_id=ctx.id,
            summary=summary,
            content=summary,
            type=mtype,
            client="test",
            tags=[],
            source_type=SOURCE_TYPE_MANUAL,
            details={"location": {"lat": lat, "lon": lon}},
            **({"deleted_at": utcnow()} if deleted else {}),
        )

    near = _mem("50m north", 35.68165, LON)  # ~50 m
    mid = _mem("500m north", 35.6857, LON)  # ~500 m
    far = _mem("about 2km away", 35.699, LON)  # ~2 km — outside 1 km radius
    tomb = _mem("tombstone at anchor", LAT, LON, deleted=True)
    timed = _mem("time-typed at anchor", LAT, LON, mtype="troubleshooting")
    plain = _mem("no location", 0, 0)
    plain.details = {"other": 1}
    db_session.add_all([near, mid, far, tomb, timed, plain])
    await db_session.flush()

    return {
        "uid": uid,
        "ws_id": ws_id,
        "ctx": ctx.id,
        "near": near.id,
        "mid": mid.id,
        "far": far.id,
        "timed": timed.id,
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_nearby_orders_by_distance_and_cuts_at_radius(geo_env, db_session):
    results = await query_nearby_memories(
        db_session, geo_env["ctx"], lat=LAT, lon=LON, radius_m=1000, k=20
    )
    ids = [r["memory_id"] for r in results]
    # timed sits at the anchor (0 m), then near, then mid; far (~2 km) and the
    # tombstone at the anchor are excluded; the location-less row never appears.
    assert ids == [str(geo_env["timed"]), str(geo_env["near"]), str(geo_env["mid"])]
    distances = [r["distance_m"] for r in results]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(0.0, abs=0.5)
    assert 30 < distances[1] < 80
    assert results[0]["type"] == "troubleshooting"  # orthogonal attribute


@pytest.mark.asyncio(loop_scope="session")
async def test_nearby_k_caps_results(geo_env, db_session):
    results = await query_nearby_memories(
        db_session, geo_env["ctx"], lat=LAT, lon=LON, radius_m=1000, k=1
    )
    assert [r["memory_id"] for r in results] == [str(geo_env["timed"])]


@pytest.mark.asyncio(loop_scope="session")
async def test_nearby_antimeridian_wraps(geo_env, db_session):
    # A row just across the dateline must be reachable from the other side.
    west = Memory(
        id=uuid.uuid4(),
        user_id=geo_env["uid"],
        workspace_id=geo_env["ws_id"],
        context_id=geo_env["ctx"],
        summary="across the dateline",
        content="x",
        type="note",
        client="test",
        tags=[],
        source_type=SOURCE_TYPE_MANUAL,
        details={"location": {"lat": 0.0, "lon": -179.9995}},
    )
    db_session.add(west)
    await db_session.flush()

    results = await query_nearby_memories(
        db_session, geo_env["ctx"], lat=0.0, lon=179.9995, radius_m=5000, k=10
    )
    assert str(west.id) in [r["memory_id"] for r in results]
    hit = next(r for r in results if r["memory_id"] == str(west.id))
    # ~111 m per 0.001° at the equator; the wrapped gap is 0.001°.
    assert 50 < hit["distance_m"] < 250


@pytest.mark.asyncio(loop_scope="session")
async def test_nearby_radius_edge_row_included(geo_env, db_session):
    # Prefilter-superset pin: a row due north at 99.99% of the radius sits in
    # the annulus a mismatched meters-per-degree constant (e.g. WGS84's
    # 111,320 vs the haversine sphere's 111,194.9) silently drops.
    import math

    from utils.geo_location import EARTH_RADIUS_M

    radius = 1000.0
    edge_lat = 0.0 + math.degrees(radius * 0.9999 / EARTH_RADIUS_M)
    edge = Memory(
        id=uuid.uuid4(),
        user_id=geo_env["uid"],
        workspace_id=geo_env["ws_id"],
        context_id=geo_env["ctx"],
        summary="row at the radius edge",
        content="x",
        type="note",
        client="test",
        tags=[],
        source_type=SOURCE_TYPE_MANUAL,
        details={"location": {"lat": edge_lat, "lon": 0.0}},
    )
    db_session.add(edge)
    await db_session.flush()

    results = await query_nearby_memories(
        db_session, geo_env["ctx"], lat=0.0, lon=0.0, radius_m=radius, k=50
    )
    hit = next((r for r in results if r["memory_id"] == str(edge.id)), None)
    assert hit is not None, "radius-edge row dropped by the bbox prefilter"
    assert 995 < hit["distance_m"] <= 1000


@pytest.mark.asyncio(loop_scope="session")
async def test_nearby_high_latitude_retrieval(geo_env, db_session):
    # The cos-corrected lon window + haversine must still retrieve near ±89.5°
    # (and the pole fallback keeps a query at 89.9999° from breaking).
    arctic = Memory(
        id=uuid.uuid4(),
        user_id=geo_env["uid"],
        workspace_id=geo_env["ws_id"],
        context_id=geo_env["ctx"],
        summary="arctic row",
        content="x",
        type="note",
        client="test",
        tags=[],
        source_type=SOURCE_TYPE_MANUAL,
        details={"location": {"lat": 89.5, "lon": 45.0}},
    )
    db_session.add(arctic)
    await db_session.flush()

    # Same latitude, 1° of longitude away — under 1 km up there.
    results = await query_nearby_memories(
        db_session, geo_env["ctx"], lat=89.5, lon=46.0, radius_m=2000, k=10
    )
    assert str(arctic.id) in [r["memory_id"] for r in results]

    # Pole-fallback query: every longitude is in reach, no error.
    results = await query_nearby_memories(
        db_session, geo_env["ctx"], lat=89.9999, lon=-170.0, radius_m=100_000, k=10
    )
    assert str(arctic.id) in [r["memory_id"] for r in results]


@pytest.mark.asyncio(loop_scope="session")
async def test_nearby_applies_binding_row_filter(geo_env, db_session):
    agent = Agent(
        workspace_id=geo_env["ws_id"],
        name=f"geo-agent-{uuid.uuid4().hex[:6]}",
        owner_user_id=geo_env["uid"],
    )
    db_session.add(agent)
    await db_session.flush()
    await AgentBindingService(db_session).create_binding(
        agent=agent,
        context_id=geo_env["ctx"],
        created_by=geo_env["uid"],
        can_read=True,
        write_policy="deny",
        is_default=True,
        allowed_memory_types=["note"],
    )
    set_agent_scope(
        AgentScope(agent_id=agent.id, enforcement_mode="enforce", workspace_id=geo_env["ws_id"])
    )
    results = await query_nearby_memories(
        db_session, geo_env["ctx"], lat=LAT, lon=LON, radius_m=1000, k=20
    )
    ids = [r["memory_id"] for r in results]
    # The troubleshooting-typed row is subtracted (#1299); notes remain.
    assert str(geo_env["timed"]) not in ids
    assert str(geo_env["near"]) in ids
