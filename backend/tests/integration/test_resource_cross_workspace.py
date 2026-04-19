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
from models.resource import ResourceSchema

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
    # A ResourceSchema row for the slug is load-bearing for the regression
    # contract: without it, /schema would return 404 simply because no
    # schema exists, and the test would pass for the wrong reason if the
    # boundary check regressed. With this row present, a regression that
    # removes required_role="owner" (or bypasses resolve_resource_by_slug
    # entirely) would return 200 + this schema's data, correctly failing
    # the 404 assertion. Copilot catch on PR #391 loop 4.
    schema_b = ResourceSchema(
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
    # Same ResourceSchema canary as the base fixture — makes the 404
    # assertion on /schema a true boundary check rather than a
    # "no-schema-exists" coincidence. See the base fixture docstring.
    schema_b = ResourceSchema(
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
