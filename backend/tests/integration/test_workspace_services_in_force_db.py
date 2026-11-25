"""The integration services in force on the workspaces page, on real rows (#3010).

- ``inspect_workspace_services`` decides with the same rule as
  ``inspect_paid_integration`` (one function), for every listed workspace in
  two statements and without a lock.
- ``GET /api/v1/me/workspace-slots`` lists them per owned workspace, by
  display name (``INTEGRATION_SERVICE_DISPLAY_NAMES``), to the owner only:
  the page lists owned workspaces, so an admin or member of the workspace
  never sees it there.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from api.main import app
from auth.dependencies import require_session_auth
from config.settings import get_settings
from db.base import get_db
from models.auth import Workspace, WorkspaceMember
from models.integration_entitlement import WorkspaceIntegrationLicense, WorkspaceIntegrationState
from services.integration_entitlements import (
    ServiceInForce,
    inspect_paid_integration,
    inspect_workspace_services,
)
from utils.datetime import utcnow

from ._admin_helpers import make_user, make_workspace

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/me/workspace-slots"
SERVICE = "example-sync"


@pytest.fixture
def mode(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    def _set(value: str) -> None:
        monkeypatch.setattr(get_settings(), "enforce_workspace_cap", value == "enforce")
        monkeypatch.setattr(get_settings(), "workspace_slot_suspension", value)

    return _set


@asynccontextmanager
async def _http(db: AsyncSession, user_id: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    principal = {"user_id": user_id, "email": f"{user_id}@example.com", "role": "user"}

    async def _db() -> AsyncGenerator[AsyncSession, None]:
        yield db

    async def _session() -> dict:
        return dict(principal, current_workspace_id=None)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_session_auth] = _session
    try:
        with patch(
            "api.routes.workspaces.get_current_user", AsyncMock(return_value=dict(principal))
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_session_auth, None)


def _state(workspace: Workspace, start: datetime, end: datetime) -> WorkspaceIntegrationState:
    return WorkspaceIntegrationState(
        workspace_id=workspace.id,
        revision=1,
        event_id="delivery-1",
        source_ref="workspace-snapshot",
        max_valid_from=start,
        max_valid_until=end,
        max_evidence_ref="paid-base-term",
    )


def _license(
    workspace: Workspace, service_id: str, start: datetime, end: datetime
) -> WorkspaceIntegrationLicense:
    return WorkspaceIntegrationLicense(
        workspace_id=workspace.id,
        service_id=service_id,
        valid_from=start,
        valid_until=end,
        evidence_ref="paid-service-term",
    )


def _z(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


async def _cases(db: AsyncSession) -> dict[str, Any]:
    """One owner with a workspace per case of the in-force rule."""
    now = utcnow().replace(microsecond=0)
    past, soon, later = now - timedelta(days=1), now + timedelta(days=3), now + timedelta(days=7)
    owner = make_user(name="Licence Owner")
    db.add(owner)
    await db.flush()
    names = (
        "in_force",
        "not_top_tier",
        "tier_ended",
        "tier_not_started",
        "license_ended",
        "license_not_started",
        "nothing",
    )
    ws = {
        name: make_workspace(
            owner_user_id=owner.user_id,
            plan_name="pro" if name == "not_top_tier" else "promax",
        )
        for name in names
    }
    db.add_all(ws.values())
    await db.flush()
    # A licence row needs its workspace's state row (foreign key).
    db.add_all(
        [
            _state(ws["in_force"], past, later),
            _state(ws["not_top_tier"], past, later),
            _state(ws["tier_ended"], past - timedelta(days=5), past),
            _state(ws["tier_not_started"], soon, later),
            _state(ws["license_ended"], past, later),
            _state(ws["license_not_started"], past, later),
        ]
    )
    await db.flush()
    db.add_all(
        [
            _license(ws["in_force"], SERVICE, past, soon),
            _license(ws["not_top_tier"], SERVICE, past, later),
            _license(ws["tier_ended"], SERVICE, past, later),
            _license(ws["tier_not_started"], SERVICE, past, later),
            _license(ws["license_ended"], SERVICE, past - timedelta(days=5), past),
            _license(ws["license_not_started"], SERVICE, soon, later),
        ]
    )
    await db.commit()
    return {"owner": owner, "ws": ws, "soon": soon, "later": later}


async def test_the_batch_read_follows_the_admission_rule_in_two_statements(
    db_session: AsyncSession,
) -> None:
    cases = await _cases(db_session)
    ws: dict[str, Workspace] = cases["ws"]
    plans = {w.id: w.plan_name for w in ws.values()}

    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(statement)

    engine = db_session.bind
    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        result = await inspect_workspace_services(db_session, plans)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert len(seen) == 2
    assert not [s for s in seen if "FOR UPDATE" in s.upper() or "FOR SHARE" in s.upper()]
    assert result == {
        ws["in_force"].id: (ServiceInForce(SERVICE, cases["soon"].replace(tzinfo=UTC)),)
    }
    # One rule: the admission's inspection agrees for every case.
    for name, workspace in ws.items():
        access = await inspect_paid_integration(db_session, workspace.id, SERVICE)
        assert access.allowed is (workspace.id in result), name


async def test_an_empty_list_reads_nothing(db_session: AsyncSession) -> None:
    assert await inspect_workspace_services(db_session, {}) == {}


async def test_the_owner_sees_services_by_display_name(
    db_session: AsyncSession, mode, monkeypatch: pytest.MonkeyPatch
) -> None:
    mode("enforce")
    now = utcnow().replace(microsecond=0)
    past, soon, later = now - timedelta(days=1), now + timedelta(days=3), now + timedelta(days=7)
    owner = make_user(name="Licence Owner")
    db_session.add(owner)
    await db_session.flush()
    top = make_workspace(owner_user_id=owner.user_id, plan_name="promax")
    plain = make_workspace(owner_user_id=owner.user_id, plan_name="free")
    db_session.add_all([top, plain])
    await db_session.flush()
    db_session.add(_state(top, past, later + timedelta(days=30)))
    await db_session.flush()
    db_session.add_all(
        [
            _license(top, "example-sync", past, soon),
            _license(top, "example-sync-eu", past, later),
            _license(top, "other-sync", past, later),
            _license(top, "ended-sync", past - timedelta(days=5), past),
        ]
    )
    await db_session.commit()
    monkeypatch.setattr(
        get_settings(),
        "integration_service_display_names",
        '{"example-sync": "Example Service", "example-sync-eu": "Example Service"}',
    )

    async with _http(db_session, owner.user_id) as client:
        response = await client.get(BASE)

    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["workspaces"]}
    assert rows[str(top.id)]["services"] == [
        {"name": "Example Service", "valid_until": _z(later)},
        {"name": "other-sync", "valid_until": _z(later)},
    ]
    assert rows[str(plain.id)]["services"] == []


@pytest.mark.parametrize("role", ["admin", "member"])
async def test_an_admin_or_member_never_gets_the_workspace(
    db_session: AsyncSession, mode, role: str
) -> None:
    """The page lists owned workspaces only: the plan and the services of a
    workspace reach its owner, never its admins or members."""
    mode("enforce")
    cases = await _cases(db_session)
    workspace = cases["ws"]["in_force"]
    other = make_user(name=f"Workspace {role}")
    db_session.add(other)
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=workspace.id, user_id=cases["owner"].user_id, role="owner"
            ),
            WorkspaceMember(workspace_id=workspace.id, user_id=other.user_id, role=role),
        ]
    )
    await db_session.commit()

    async with _http(db_session, other.user_id) as client:
        theirs = await client.get(BASE)
    async with _http(db_session, cases["owner"].user_id) as client:
        owners = await client.get(BASE)

    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["workspaces"] == []
    owned = {row["id"]: row for row in owners.json()["workspaces"]}
    assert owned[str(workspace.id)]["services"] == [
        {"name": SERVICE, "valid_until": _z(cases["soon"])}
    ]
