"""Cross-workspace probe tests for resources read endpoints (Issue #389).

Regression prevention for the CWE-639 / OWASP A01 pattern: an owner of one
workspace must not be able to surface a slug that lives only in another
workspace. ``PermissionService.resolve_resource_by_slug`` is the layer that
enforces this — it returns uniform 404 (not 403) so cross-workspace
existence does not leak.

The companion unit test ``tests/api/test_resource_owner_gate.py`` covers
the 403 path (same-workspace non-owner) via ``dependency_overrides``.
This file exercises the 404 path against a real database so the contract
is verified end-to-end and future refactors (e.g., the Phase 2 writer
migration in #390) do not accidentally regress the boundary check.

Scope note: this file is intentionally narrow — a single 404 assertion
per slug-path endpoint. It is not trying to re-test every response shape
or the downstream satellite-read logic. The unit test covers 403;
existing feature tests cover the happy path.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from auth.dependencies import (
    get_user_from_api_key_or_session,
    require_workspace_owner,
)
from db.base import get_db
from models.auth import Context, Workspace, WorkspaceMember
from models.resource import Resource, ResourceSchema, ResourceToken
from utils.datetime import utcnow

SLUG_IN_WORKSPACE_B = "ws_b_only_slug"


def _make_fresh_session_override(engine):
    """Yield a fresh AsyncSession per request for the given engine.

    Copilot catch on PR #391 loop 3: yielding the pytest-scoped
    ``db_session`` directly into the FastAPI app runs the HTTP call in
    ``TestClient``'s own event loop/thread, which can raise cross-event-loop
    or cross-thread errors on the reused session. Creating a fresh session
    via ``async_sessionmaker(engine)`` per request avoids this — it mirrors
    the reference pattern in ``backend/tests/api/test_api_integration.py``.
    Setup data committed on the pytest-scoped ``db_session`` is visible to
    the fresh sessions because they share the same underlying DB.
    """
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_maker() as session:
            try:
                yield session
            finally:
                await session.rollback()

    return override_get_db


@pytest_asyncio.fixture
async def cross_workspace_scenario(async_engine, db_session):
    """Create two workspaces where user_A owns A and a resource slug lives only in B.

    Yields the user_A identifier + workspace_A id. A probe from user_A for
    ``SLUG_IN_WORKSPACE_B`` should produce 404 uniform disclosure.
    """
    owner_a_id = f"owner_a_{uuid4().hex[:8]}"
    owner_b_id = f"owner_b_{uuid4().hex[:8]}"

    ws_a = Workspace(
        id=uuid4(),
        name=f"ws-a-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_a_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ws_b = Workspace(
        id=uuid4(),
        name=f"ws-b-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_b_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ctx_b = Context(
        id=uuid4(),
        workspace_id=ws_b.id,
        name=f"ctx-b-{uuid4().hex[:8]}",
        resource_id=SLUG_IN_WORKSPACE_B,
        created_by=owner_b_id,
    )
    # Issue #390 Phase 2: Resource entity row for workspace B so the
    # canary ResourceSchema below can carry a valid ``resource_pk`` — the
    # before_insert event listener on ResourceSchema rejects rows with
    # only ``resource_id`` set.
    resource_b = Resource(
        id=uuid4(),
        workspace_id=ws_b.id,
        resource_id=SLUG_IN_WORKSPACE_B,
        name="ws-b-canary",
        created_by=owner_b_id,
    )
    # A ResourceSchema row for the slug is load-bearing for the regression
    # contract: without it, /schema would return 404 simply because no
    # schema exists, and the test would pass for the wrong reason if the
    # boundary check regressed. With this row present, a regression that
    # removes required_role="owner" (or bypasses resolve_resource_by_slug
    # entirely) would return 200 + this schema's data, correctly failing
    # the 404 assertion. Copilot catch on PR #391 loop 4.
    schema_b = ResourceSchema(
        resource_pk=resource_b.id,
        resource_id=SLUG_IN_WORKSPACE_B,
        schema_version=1,
        field_definitions=[{"name": "canary_field", "type": "text"}],
    )
    db_session.add_all(
        [
            ws_a,
            ws_b,
            WorkspaceMember(workspace_id=ws_a.id, user_id=owner_a_id, role="owner"),
            WorkspaceMember(workspace_id=ws_b.id, user_id=owner_b_id, role="owner"),
            resource_b,
            ctx_b,
            schema_b,
        ]
    )
    await db_session.commit()

    async def override_auth():
        return {
            "user_id": owner_a_id,
            "email": f"{owner_a_id}@test.com",
            "role": "user",
            "current_workspace_id": ws_a.id,
            "workspace_role": "owner",
        }

    async def override_require_workspace_owner():
        # Skip the real role check (owner of A is confirmed above) and
        # return the (user_id, workspace_id) tuple the handlers unpack.
        return (owner_a_id, ws_a.id)

    app.dependency_overrides[get_db] = _make_fresh_session_override(async_engine)
    app.dependency_overrides[get_user_from_api_key_or_session] = override_auth
    app.dependency_overrides[require_workspace_owner] = override_require_workspace_owner

    yield {
        "owner_a_id": owner_a_id,
        "ws_a_id": ws_a.id,
        "probe_slug": SLUG_IN_WORKSPACE_B,
    }

    app.dependency_overrides.clear()

    # Cleanup — commit persisted data; don't leak into other tests. Re-raise
    # on failure so cleanup problems surface as test failures rather than
    # silently accumulating rows in the shared test DB (per-PR #391 Copilot
    # catch: swallowed cleanup exceptions cause hard-to-diagnose follow-on
    # failures).
    try:
        await db_session.execute(
            ResourceSchema.__table__.delete().where(
                ResourceSchema.resource_id == SLUG_IN_WORKSPACE_B
            )
        )
        await db_session.delete(ctx_b)
        # Delete Resource after its satellite rows — CASCADE would also work
        # but being explicit keeps the teardown deterministic.
        await db_session.execute(Resource.__table__.delete().where(Resource.id == resource_b.id))
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(
                WorkspaceMember.workspace_id.in_([ws_a.id, ws_b.id])
            )
        )
        await db_session.delete(ws_a)
        await db_session.delete(ws_b)
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


SLUG_PATH_ENDPOINTS = [
    "/api/v1/resources/{slug}/schema",
    "/api/v1/resources/{slug}/impact",
    "/api/v1/resources/{slug}/indexer-status",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path_template", SLUG_PATH_ENDPOINTS)
async def test_cross_workspace_probe_returns_404(cross_workspace_scenario, path_template):
    """Owner of workspace A probing workspace B's slug must get 404 (not 200, not 403).

    404 is CWE-639 uniform disclosure — identical to the "slug does not exist"
    response so the caller cannot distinguish "workspace B exists" from "nothing
    by that name anywhere". 403 would leak existence across workspace boundaries.
    """
    slug = cross_workspace_scenario["probe_slug"]
    path = path_template.format(slug=slug)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path)

    assert response.status_code == 404, (
        f"GET {path} returned {response.status_code}, expected 404 "
        f"(CWE-639 uniform disclosure contract)"
    )


# ============================================================================
# Multi-workspace membership case (Copilot catch on PR #391)
# ============================================================================
#
# The base fixture covers "owner of A, NOT a member of B". The more subtle
# case is "owner of A, also a member/admin of B". Without required_role="owner"
# on resolve_resource_by_slug, WorkspaceOwner only verifies ownership of the
# caller's *current* workspace (A), while the helper's default required_role=
# "member" would pass the B-membership check and leak B's resource data.
# Pinning this case prevents the bug from being reintroduced.


@pytest_asyncio.fixture
async def cross_workspace_multi_member_scenario(async_engine, db_session):
    """user_a owns workspace A AND is a ``member`` of workspace B.

    Probes for ``SLUG_IN_WORKSPACE_B`` from user_a must still return 404 —
    the owner-of-A current-workspace check is not enough; the helper must
    enforce owner role in the *resource's* owning workspace.
    """
    owner_a_id = f"owner_a_{uuid4().hex[:8]}"
    owner_b_id = f"owner_b_{uuid4().hex[:8]}"

    ws_a = Workspace(
        id=uuid4(),
        name=f"ws-a-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_a_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ws_b = Workspace(
        id=uuid4(),
        name=f"ws-b-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_b_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ctx_b = Context(
        id=uuid4(),
        workspace_id=ws_b.id,
        name=f"ctx-b-{uuid4().hex[:8]}",
        resource_id=SLUG_IN_WORKSPACE_B,
        created_by=owner_b_id,
    )
    # Issue #390 Phase 2: Resource entity row to back the schema canary.
    resource_b = Resource(
        id=uuid4(),
        workspace_id=ws_b.id,
        resource_id=SLUG_IN_WORKSPACE_B,
        name="ws-b-canary",
        created_by=owner_b_id,
    )
    # Same ResourceSchema canary as the base fixture — makes the 404
    # assertion on /schema a true boundary check rather than a
    # "no-schema-exists" coincidence. See the base fixture docstring.
    schema_b = ResourceSchema(
        resource_pk=resource_b.id,
        resource_id=SLUG_IN_WORKSPACE_B,
        schema_version=1,
        field_definitions=[{"name": "canary_field", "type": "text"}],
    )
    # user_a is owner of A AND member of B — the subtle case.
    db_session.add_all(
        [
            ws_a,
            ws_b,
            WorkspaceMember(workspace_id=ws_a.id, user_id=owner_a_id, role="owner"),
            WorkspaceMember(workspace_id=ws_b.id, user_id=owner_b_id, role="owner"),
            WorkspaceMember(workspace_id=ws_b.id, user_id=owner_a_id, role="member"),
            resource_b,
            ctx_b,
            schema_b,
        ]
    )
    await db_session.commit()

    async def override_auth():
        return {
            "user_id": owner_a_id,
            "email": f"{owner_a_id}@test.com",
            "role": "user",
            "current_workspace_id": ws_a.id,
            "workspace_role": "owner",
        }

    async def override_require_workspace_owner():
        return (owner_a_id, ws_a.id)

    app.dependency_overrides[get_db] = _make_fresh_session_override(async_engine)
    app.dependency_overrides[get_user_from_api_key_or_session] = override_auth
    app.dependency_overrides[require_workspace_owner] = override_require_workspace_owner

    yield {
        "owner_a_id": owner_a_id,
        "ws_a_id": ws_a.id,
        "probe_slug": SLUG_IN_WORKSPACE_B,
    }

    app.dependency_overrides.clear()

    # See the base fixture's cleanup comment — re-raise on failure so
    # accumulated rows in the shared test DB surface deterministically.
    try:
        await db_session.execute(
            ResourceSchema.__table__.delete().where(
                ResourceSchema.resource_id == SLUG_IN_WORKSPACE_B
            )
        )
        await db_session.delete(ctx_b)
        await db_session.execute(Resource.__table__.delete().where(Resource.id == resource_b.id))
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(
                WorkspaceMember.workspace_id.in_([ws_a.id, ws_b.id])
            )
        )
        await db_session.delete(ws_a)
        await db_session.delete(ws_b)
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


@pytest.mark.asyncio
@pytest.mark.parametrize("path_template", SLUG_PATH_ENDPOINTS)
async def test_multi_workspace_member_probe_returns_404(
    cross_workspace_multi_member_scenario, path_template
):
    """Owner-of-A who is also a member-of-B must still get 404 on B's slug.

    Regression pin for the required_role="owner" fix on resolve_resource_by_slug.
    Without it, the helper's default required_role="member" would pass for this
    caller and leak workspace B's resource data.
    """
    slug = cross_workspace_multi_member_scenario["probe_slug"]
    path = path_template.format(slug=slug)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path)

    assert response.status_code == 404, (
        f"GET {path} returned {response.status_code}, expected 404 "
        f"(multi-workspace member must not leak via resolve_resource_by_slug "
        f"default required_role=member)"
    )


# ============================================================================
# Issue #390 Phase 2: list endpoint body-inspection + slug-reuse scenarios
# ============================================================================
#
# The parametrized slug-path tests above verify 404 uniform disclosure on
# per-resource endpoints. The list endpoints (``/api/v1/resources`` and
# ``/api/v1/resource-tokens``) return 200 with a possibly-empty body, so
# status code alone cannot distinguish "properly isolated" from "leaked".
# These tests inspect the response body directly.
#
# The slug-reuse test exercises the core exploit vector #390 closes:
# workspace A soft-deletes a Context with slug ``x``; workspace B then
# creates a Resource with slug ``x``. Without the Phase 2 writer +
# read-path migration, any orphan satellite rows from workspace A (still
# keyed by slug) would surface under workspace B's reads.


@pytest_asyncio.fixture
async def cross_workspace_list_scenario(async_engine, db_session):
    """Owner-of-A probes list endpoints while workspace B has a live resource.

    Yields auth overrides that make the caller owner of workspace A. The
    list endpoints called under these overrides must NOT return any row
    whose data came from workspace B (identifiable by the canary resource
    ``ws_b_list_canary``).
    """
    owner_a_id = f"owner_a_{uuid4().hex[:8]}"
    owner_b_id = f"owner_b_{uuid4().hex[:8]}"
    canary_slug = f"ws_b_list_canary_{uuid4().hex[:8]}"

    ws_a = Workspace(
        id=uuid4(),
        name=f"ws-a-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_a_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ws_b = Workspace(
        id=uuid4(),
        name=f"ws-b-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_b_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    resource_b = Resource(
        id=uuid4(),
        workspace_id=ws_b.id,
        resource_id=canary_slug,
        name="ws-b-list-canary",
        created_by=owner_b_id,
    )
    ctx_b = Context(
        id=uuid4(),
        workspace_id=ws_b.id,
        name=f"ctx-b-{uuid4().hex[:8]}",
        resource_id=canary_slug,
        created_by=owner_b_id,
    )
    token_b = ResourceToken(
        resource_pk=resource_b.id,
        resource_id=canary_slug,
        workspace_id=ws_b.id,
        token_hash="canary_hash_" + uuid4().hex,
        description="ws-b list canary token",
        quota_events_per_hour=100,
        created_by=owner_b_id,
    )

    db_session.add_all(
        [
            ws_a,
            ws_b,
            WorkspaceMember(workspace_id=ws_a.id, user_id=owner_a_id, role="owner"),
            WorkspaceMember(workspace_id=ws_b.id, user_id=owner_b_id, role="owner"),
            resource_b,
            ctx_b,
            token_b,
        ]
    )
    await db_session.commit()

    async def override_auth():
        return {
            "user_id": owner_a_id,
            "email": f"{owner_a_id}@test.com",
            "role": "user",
            "current_workspace_id": ws_a.id,
            "workspace_role": "owner",
        }

    async def override_require_workspace_owner():
        return (owner_a_id, ws_a.id)

    app.dependency_overrides[get_db] = _make_fresh_session_override(async_engine)
    app.dependency_overrides[get_user_from_api_key_or_session] = override_auth
    app.dependency_overrides[require_workspace_owner] = override_require_workspace_owner

    yield {
        "owner_a_id": owner_a_id,
        "ws_a_id": ws_a.id,
        "canary_slug": canary_slug,
    }

    app.dependency_overrides.clear()

    try:
        await db_session.execute(
            ResourceToken.__table__.delete().where(ResourceToken.id == token_b.id)
        )
        await db_session.delete(ctx_b)
        await db_session.execute(Resource.__table__.delete().where(Resource.id == resource_b.id))
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(
                WorkspaceMember.workspace_id.in_([ws_a.id, ws_b.id])
            )
        )
        await db_session.delete(ws_a)
        await db_session.delete(ws_b)
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


@pytest.mark.asyncio
async def test_resources_list_excludes_other_workspace_rows(cross_workspace_list_scenario):
    """GET /api/v1/resources from owner-of-A must not include workspace B's canary.

    Status code alone (200) does not distinguish isolated from leaky on a
    list endpoint — we must inspect the body. The canary slug lives only
    in workspace B; workspace A's response body must not contain it.
    """
    canary = cross_workspace_list_scenario["canary_slug"]

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/resources")

    assert response.status_code == 200, f"Unexpected status: {response.status_code}"
    data = response.json()
    resource_ids = [r["resource_id"] for r in data.get("resources", [])]
    assert canary not in resource_ids, (
        f"Cross-workspace leak: GET /api/v1/resources from owner-of-A "
        f"returned workspace B's canary slug '{canary}'. "
        f"Full response: {resource_ids}"
    )


@pytest.mark.asyncio
async def test_slug_reuse_after_soft_delete_isolates_orphans(async_engine, db_session):
    """Workspace A soft-deletes slug ``x``; workspace B creates slug ``x``.

    Owner-of-B reading ``/resources/x/schema`` must see only B's schema —
    workspace A's orphan ResourceSchema (still keyed by slug + stale
    resource_pk) must not surface. This is the core CWE-639 exploit
    vector that #390 Phase 2 closes via strict ``resource_pk`` read
    filtering.
    """
    shared_slug = f"reused_slug_{uuid4().hex[:8]}"

    owner_a_id = f"owner_a_{uuid4().hex[:8]}"
    owner_b_id = f"owner_b_{uuid4().hex[:8]}"

    ws_a = Workspace(
        id=uuid4(),
        name=f"ws-a-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_a_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ws_b = Workspace(
        id=uuid4(),
        name=f"ws-b-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_b_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )

    # Workspace A: resource + soft-deleted context + orphan ResourceSchema.
    resource_a = Resource(
        id=uuid4(),
        workspace_id=ws_a.id,
        resource_id=shared_slug,
        name="ws-a-original",
        created_by=owner_a_id,
    )
    ctx_a_soft_deleted = Context(
        id=uuid4(),
        workspace_id=ws_a.id,
        name=f"ctx-a-{uuid4().hex[:8]}",
        resource_id=shared_slug,
        created_by=owner_a_id,
    )
    # Mark as soft-deleted — releases the slug under the partial UNIQUE
    # index ``deleted_at IS NULL``.
    ctx_a_soft_deleted.deleted_at = utcnow()
    schema_a_orphan = ResourceSchema(
        resource_pk=resource_a.id,
        resource_id=shared_slug,
        schema_version=1,
        field_definitions=[{"name": "ws_a_orphan_canary", "type": "text"}],
    )

    # Workspace B: new resource with same slug + its own schema.
    resource_b = Resource(
        id=uuid4(),
        workspace_id=ws_b.id,
        resource_id=shared_slug,
        name="ws-b-reused",
        created_by=owner_b_id,
    )
    ctx_b = Context(
        id=uuid4(),
        workspace_id=ws_b.id,
        name=f"ctx-b-{uuid4().hex[:8]}",
        resource_id=shared_slug,
        created_by=owner_b_id,
    )
    schema_b = ResourceSchema(
        resource_pk=resource_b.id,
        resource_id=shared_slug,
        schema_version=1,
        field_definitions=[{"name": "ws_b_correct_field", "type": "text"}],
    )

    db_session.add_all(
        [
            ws_a,
            ws_b,
            WorkspaceMember(workspace_id=ws_a.id, user_id=owner_a_id, role="owner"),
            WorkspaceMember(workspace_id=ws_b.id, user_id=owner_b_id, role="owner"),
            resource_a,
            resource_b,
            ctx_a_soft_deleted,
            ctx_b,
            schema_a_orphan,
            schema_b,
        ]
    )
    await db_session.commit()

    async def override_auth():
        return {
            "user_id": owner_b_id,
            "email": f"{owner_b_id}@test.com",
            "role": "user",
            "current_workspace_id": ws_b.id,
            "workspace_role": "owner",
        }

    async def override_require_workspace_owner():
        return (owner_b_id, ws_b.id)

    app.dependency_overrides[get_db] = _make_fresh_session_override(async_engine)
    app.dependency_overrides[get_user_from_api_key_or_session] = override_auth
    app.dependency_overrides[require_workspace_owner] = override_require_workspace_owner

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(f"/api/v1/resources/{shared_slug}/schema")

        assert response.status_code == 200, (
            f"Owner-of-B probing shared slug should see their own schema; "
            f"got {response.status_code}"
        )
        data = response.json()
        fields = [f.get("name") for f in data.get("field_definitions", [])]
        assert "ws_b_correct_field" in fields, (
            f"Expected workspace B's schema, got fields: {fields}"
        )
        assert "ws_a_orphan_canary" not in fields, (
            f"Cross-workspace leak: workspace A's orphan schema surfaced in "
            f"workspace B's response. Fields returned: {fields}"
        )
    finally:
        app.dependency_overrides.clear()
        try:
            await db_session.execute(
                ResourceSchema.__table__.delete().where(ResourceSchema.resource_id == shared_slug)
            )
            await db_session.delete(ctx_a_soft_deleted)
            await db_session.delete(ctx_b)
            await db_session.execute(
                Resource.__table__.delete().where(Resource.id.in_([resource_a.id, resource_b.id]))
            )
            await db_session.execute(
                WorkspaceMember.__table__.delete().where(
                    WorkspaceMember.workspace_id.in_([ws_a.id, ws_b.id])
                )
            )
            await db_session.delete(ws_a)
            await db_session.delete(ws_b)
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise


@pytest.mark.asyncio
async def test_resource_tokens_list_excludes_other_workspace_rows(
    cross_workspace_list_scenario,
):
    """GET /api/v1/resource-tokens from owner-of-A with workspace-B canary.

    The REST list endpoint filters by ``created_by=user_id`` at the manager
    layer. Because owner_b (not owner_a) created the canary token in
    workspace B, owner_a's list should be empty regardless of workspace
    scoping. The assertion also defends against a future refactor that
    switches to a slug-based filter without a workspace boundary: any
    rows surfacing here with the canary ``resource_id`` would prove a
    cross-workspace leak.
    """
    canary = cross_workspace_list_scenario["canary_slug"]

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/resource-tokens")

    assert response.status_code == 200, f"Unexpected status: {response.status_code}"
    data = response.json()
    token_resource_ids = [t["resource_id"] for t in data.get("tokens", [])]
    assert canary not in token_resource_ids, (
        f"Cross-workspace leak: GET /api/v1/resource-tokens from owner-of-A "
        f"returned workspace B's canary token ({canary!r}). "
        f"Full response resource_ids: {token_resource_ids}"
    )
