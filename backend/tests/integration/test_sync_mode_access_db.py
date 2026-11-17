"""Normal human lifecycle access remains available without granting data access."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.exc import DBAPIError

from api.routes import me_workspaces
from models.auth import Context, ContextMember, IdentityLink, Workspace, WorkspaceMember
from models.memory import Memory
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from models.public_mcp import PublicMcpEndpoint
from models.sync_installation import SyncInstallation, SyncService
from models.sync_management import SyncManagedTarget, SyncModeOperation
from services.capacity_lock import ENTITLEMENT_SOURCE_EXTERNAL_BILLING
from services.context_service import ContextService
from services.identity_link_service import IdentityLinkService
from services.permission_service import PermissionService
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import human, request
from utils.db_helpers import is_lock_not_available

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, managed = (
    management.store,
    management.worker,
    management.feeds,
    management.managed,
)


@pytest_asyncio.fixture(loop_scope="session")
async def subject(managed):
    managed.store.app.include_router(me_workspaces.router, prefix="/api/v1")
    yield managed
    async with managed.factory() as db:
        await db.execute(delete(Memory).where(Memory.workspace_id == managed.store.wid))
        await db.commit()


async def footprint(subject):
    async with subject.factory() as db:
        installation = await db.get(SyncInstallation, subject.iid)
        target = await db.get(SyncManagedTarget, subject.iid)
        context = await db.get(Context, UUID(subject.target["context_id"]))
        return (
            installation.generation,
            installation.state,
            None
            if target is None
            else tuple(
                getattr(target, key)
                for key in ("mode", "epoch", "generation", "stop_event_id", "event_floor")
            ),
            context.sleep_mode,
            await db.scalar(
                select(func.count())
                .select_from(SyncModeOperation)
                .where(SyncModeOperation.installation_id == subject.iid)
            ),
        )


async def private_target(subject, *, linked=False):
    await management.manual(subject)
    context_id = UUID(subject.target["context_id"])
    async with subject.factory() as owner:
        service = ContextService(owner)
        await service.update_context(subject.store.users["owner"], context_id, is_public=False)
        if linked:
            await service.update_context(subject.store.users["owner"], context_id, is_private=True)
            await IdentityLinkService(owner).link(
                subject.store.users["owner"], subject.store.users["admin"]
            )
    return context_id


async def test_profile_refreshes_context_after_owner_privacy_change(subject, monkeypatch):
    context_id = await private_target(subject)
    original = management.sync_management.target_fence

    async def racing_privacy(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with subject.factory() as owner:
            await ContextService(owner).update_context(
                subject.store.users["owner"], context_id, is_private=True
            )
        return result

    monkeypatch.setattr(management.sync_management, "target_fence", racing_privacy)
    response = await human(subject, "GET", "management/access", role="admin")
    assert response.status_code == 404, response.text
    private(response)


async def test_profile_fences_actual_unlink_until_its_observation_commits(subject, monkeypatch):
    await private_target(subject, linked=True)
    original = PermissionService.check_context_access
    checks, blocked = 0, False

    async def racing_unlink(self, *args, **kwargs):
        nonlocal checks, blocked
        result = await original(self, *args, **kwargs)
        if args[0] == subject.store.users["admin"]:
            checks += 1
            if checks == 3:
                async with subject.factory() as owner:
                    await owner.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    try:
                        await IdentityLinkService(owner).unlink(
                            subject.store.users["owner"], subject.store.users["admin"]
                        )
                    except DBAPIError as exc:
                        assert is_lock_not_available(exc)
                        await owner.rollback()
                        blocked = True
        return result

    monkeypatch.setattr(PermissionService, "check_context_access", racing_unlink)
    response = await human(subject, "GET", "management/access", role="admin")
    assert checks == 3
    assert blocked, "Unlink committed after final RBAC before lifecycle capability observation"
    profile(subject, response, "admin", available=False)
    async with subject.factory() as owner:
        await IdentityLinkService(owner).unlink(
            subject.store.users["owner"], subject.store.users["admin"]
        )
    assert (await human(subject, "GET", "management/access", role="admin")).status_code == 404


@pytest.mark.parametrize("held", ["context", "link"])
async def test_profile_authority_contention_never_returns_a_stale_capability(subject, held):
    context_id = await private_target(subject, linked=True)
    async with subject.factory() as writer:
        model, condition = (
            (Context, Context.id == context_id)
            if held == "context"
            else (IdentityLink, IdentityLink.user_id == subject.store.users["admin"])
        )
        await writer.scalar(select(model).where(condition).with_for_update())
        response = await human(subject, "GET", "management/access", role="admin")
        assert response.status_code == 409, response.text
        assert "permissions" not in response.text
        await writer.rollback()


async def normal_access(subject, role="owner"):
    return await request(
        subject,
        "GET",
        f"/api/v1/me/workspaces/{subject.store.wid}/access",
        token=subject.store.tokens[role],
        params={"context_id": subject.target["context_id"]},
    )


def private(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def profile(subject, response, role="owner", allowed=True, available=True):
    assert response.status_code == 200, response.text
    private(response)
    value = response.json()
    assert set(value) == {"contract_version", "subject", "target", "management", "permissions"}
    assert value["contract_version"] == "sync-mode-access-v1"
    assert value["subject"] == subject.store.users[role]
    assert value["target"] == {
        "workspace_id": str(subject.store.wid),
        "installation_id": str(subject.iid),
        "resource_pk": subject.target["resource_pk"],
        "context_id": subject.target["context_id"],
        "endpoint_id": subject.target["endpoint_id"],
        "generation": value["management"]["generation"],
        "target_available": available,
    }
    assert value["permissions"] == {"read": True, "stop": allowed, "finalize": allowed}
    assert not any(
        marker in response.text
        for marker in ("access_token", "refresh_token", "public_path", "source_url", "resource_id")
    )
    return value


@pytest.mark.parametrize("role", ["owner", "member", "viewer", "outsider"])
async def test_current_normal_profile_uses_context_role_and_has_no_side_effect(subject, role):
    before = await footprint(subject)
    response = await human(subject, "GET", "management/access", role=role)
    if role == "outsider":
        assert response.status_code == 404, response.text
        private(response)
    else:
        value = profile(subject, response, role, role != "viewer")
        assert value["management"] == {
            "mode": "feed",
            "epoch": 1,
            "generation": 1,
            "stop_event_id": 0,
            "reason": None,
        }
    assert await footprint(subject) == before


@pytest.mark.parametrize("mode", ["stopping", "manual"])
async def test_profile_reports_retained_current_management_and_generation(subject, mode):
    await management.stop(subject)
    if mode == "manual":
        await management.finalize(subject)
    before = await footprint(subject)
    value = profile(subject, await human(subject, "GET", "management/access"))
    assert value["management"]["mode"] == mode
    assert value["target"]["generation"] == 2
    assert await footprint(subject) == before


@pytest.mark.parametrize("role", ["owner", "member"])
async def test_actual_capacity_lock_keeps_safe_lifecycle_access_and_data_denied(subject, role):
    async with subject.factory() as db:
        workspace = await db.get(Workspace, subject.store.wid)
        workspace.plan_name = "free"
        workspace.entitlement_source = ENTITLEMENT_SOURCE_EXTERNAL_BILLING
        await db.flush()
        count = workspace.effective_memory_limit + 1
        assert 0 < count <= 20000
        await db.execute(
            insert(Memory),
            [
                {
                    "user_id": subject.store.users["owner"],
                    "workspace_id": subject.store.wid,
                    "content": "Capacity fixture, never returned by access metadata",
                    "summary": "Capacity fixture",
                    "type": "note",
                    "client": "pytest",
                }
                for _ in range(count)
            ],
        )
        await db.commit()
    ordinary = await normal_access(subject, role)
    assert ordinary.status_code == 200, ordinary.text
    assert ordinary.json()["resource"]["permissions"] == {"read": False, "sync.manage": False}
    before = await footprint(subject)
    profile(subject, await human(subject, "GET", "management/access", role=role), role)
    assert await footprint(subject) == before
    assert (await normal_access(subject, role)).json() == ordinary.json()
    assert (
        await human(
            subject, "POST", "management/stop", role=role, body={"operation_id": str(uuid4())}
        )
    ).status_code == 200
    assert (
        await human(
            subject, "POST", "management/finalize", role=role, body={"operation_id": str(uuid4())}
        )
    ).status_code == 200


@pytest.mark.parametrize("role", ["owner", "member"])
async def test_actual_context_slot_suspension_does_not_remove_safe_stop_access(subject, role):
    async with subject.factory() as db:
        workspace = await db.get(Workspace, subject.store.wid)
        workspace.plan_name = "free"
        workspace.entitlement_source = ENTITLEMENT_SOURCE_EXTERNAL_BILLING
        target = await db.get(Context, UUID(subject.target["context_id"]))
        for index in range(workspace.effective_max_contexts):
            db.add(
                Context(
                    workspace_id=workspace.id,
                    name=f"older-mode-slot-{index}",
                    created_by=subject.store.users["owner"],
                    created_at=target.created_at - timedelta(days=1),
                    is_private=True,
                )
            )
        await db.commit()
    ordinary = await normal_access(subject, role)
    assert ordinary.status_code == 200, ordinary.text
    assert ordinary.json()["resource"]["permissions"] == {"read": True, "sync.manage": False}
    before = await footprint(subject)
    profile(subject, await human(subject, "GET", "management/access", role=role), role)
    assert await footprint(subject) == before
    assert (await normal_access(subject, role)).json() == ordinary.json()


async def test_read_profile_does_not_turn_an_ordinary_token_into_a_control_grant(subject):
    async with subject.factory() as db:
        for model, key in (
            (OIDCGrant, subject.store.grant_ids["owner"]),
            (OIDCToken, subject.store.token_ids["owner"]),
        ):
            row = await db.get(model, key)
            row.scope = "openid workspaces:read"
        await db.commit()
    before = await footprint(subject)
    profile(subject, await human(subject, "GET", "management/access"))
    denied = await human(subject, "POST", "management/stop", body={"operation_id": str(uuid4())})
    assert denied.status_code == 403, denied.text
    assert await footprint(subject) == before


async def test_profile_requires_the_workspace_read_scope_on_the_live_token(subject):
    async with subject.factory() as db:
        for model, key in (
            (OIDCGrant, subject.store.grant_ids["owner"]),
            (OIDCToken, subject.store.token_ids["owner"]),
        ):
            row = await db.get(model, key)
            row.scope = "openid integrations:manage"
        await db.commit()
    before = await footprint(subject)
    response = await human(subject, "GET", "management/access")
    assert response.status_code == 403, response.text
    private(response)
    assert "permissions" not in response.text
    assert await footprint(subject) == before


async def test_credential_expiry_during_profile_observation_is_an_error_not_a_profile(
    subject, monkeypatch
):
    from services import sync_provisioning

    original = sync_provisioning.installation_view

    async def expiring(*args, **kwargs):
        subject.store.clock.now += timedelta(days=2)
        return await original(*args, **kwargs)

    monkeypatch.setattr(sync_provisioning, "installation_view", expiring)
    before = await footprint(subject)
    response = await human(subject, "GET", "management/access")
    assert response.status_code == 409, response.text
    private(response)
    assert "permissions" not in response.text
    assert await footprint(subject) == before


async def test_fresh_role_and_membership_are_checked_for_each_profile(subject):
    profile(subject, await human(subject, "GET", "management/access", role="member"), "member")
    async with subject.factory() as db:
        row = await db.scalar(
            select(ContextMember).where(
                ContextMember.context_id == UUID(subject.target["context_id"]),
                ContextMember.user_id == subject.store.users["member"],
            )
        )
        row.role = "viewer"
        await db.commit()
    profile(
        subject, await human(subject, "GET", "management/access", role="member"), "member", False
    )
    async with subject.factory() as db:
        await db.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == subject.store.wid,
                WorkspaceMember.user_id == subject.store.users["member"],
            )
        )
        await db.commit()
    assert (await human(subject, "GET", "management/access", role="member")).status_code == 404


@pytest.mark.parametrize("revoked", ["token", "grant", "client", "registration"])
async def test_revoked_identity_or_wrong_registered_client_never_keeps_access(subject, revoked):
    async with subject.factory() as db:
        if revoked == "registration":
            service = await db.get(SyncService, subject.store.service_id)
            service.browser_client_id = subject.store.foreign_client
        else:
            model, key = {
                "token": (OIDCToken, subject.store.token_ids["owner"]),
                "grant": (OIDCGrant, subject.store.grant_ids["owner"]),
                "client": (OIDCClient, subject.store.client_id),
            }[revoked]
            row = await db.get(model, key)
            row.revoked_at = subject.store.clock.now
        await db.commit()
    response = await human(subject, "GET", "management/access")
    assert response.status_code == (404 if revoked == "registration" else 401), response.text
    private(response)


@pytest.mark.parametrize("foreign", ["workspace", "installation"])
async def test_foreign_fixed_identity_is_not_a_target_selector(subject, foreign):
    before = await footprint(subject)
    response = await human(
        subject,
        "GET",
        "management/access",
        workspace=subject.store.other if foreign == "workspace" else None,
        installation=uuid4() if foreign == "installation" else None,
    )
    assert response.status_code == 404, response.text
    private(response)
    assert await footprint(subject) == before


async def test_worker_credentials_and_unknown_selectors_never_become_mode_authority(subject):
    root = (
        f"/api/v1/workspaces/{subject.store.wid}/sync-installations/{subject.iid}/management/access"
    )
    worker_response = await request(subject, "GET", root, token=subject.token)
    assert worker_response.status_code == 401, worker_response.text
    private(worker_response)
    response = await human(
        subject, "GET", "management/access", params={"scope": "never-echo-profile-input"}
    )
    assert response.status_code == 400, response.text
    assert "never-echo-profile-input" not in response.text
    private(response)


async def test_unenrolled_profile_reads_default_state_without_enrollment_or_receipt(subject):
    async with subject.factory() as db:
        await db.delete(await db.get(SyncManagedTarget, subject.iid))
        await db.commit()
    before = await footprint(subject)
    assert before[2] is None
    value = profile(subject, await human(subject, "GET", "management/access"))
    assert value["management"] == {
        "mode": "feed",
        "epoch": 1,
        "generation": 1,
        "stop_event_id": 0,
        "reason": None,
    }
    assert await footprint(subject) == before


async def test_unpublished_endpoint_reports_loss_without_repair_or_authority_change(subject):
    async with subject.factory() as db:
        endpoint = await db.get(PublicMcpEndpoint, UUID(subject.target["endpoint_id"]))
        endpoint.revoked_at = subject.store.clock.now
        await db.commit()
    before = await footprint(subject)
    profile(subject, await human(subject, "GET", "management/access"), available=False)
    assert await footprint(subject) == before


async def test_disabled_management_profile_returns_private_not_found_before_opening_db(
    subject, monkeypatch
):
    from db.base import get_db
    from services import sync_management

    async def forbidden_db():
        raise AssertionError("Disabled metadata must not open a database session")
        yield  # pragma: no cover

    app = subject.store.app
    previous = app.dependency_overrides[get_db]
    before = await footprint(subject)
    monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", False)
    app.dependency_overrides[get_db] = forbidden_db
    try:
        response = await human(subject, "GET", "management/access")
        assert response.status_code == 404, response.text
        private(response)
    finally:
        app.dependency_overrides[get_db] = previous
    assert await footprint(subject) == before


@pytest.mark.parametrize("changed", ["installation_id", "resource_pk", "context_id", "generation"])
async def test_mismatched_management_binding_never_becomes_an_access_profile(subject, changed):
    async with subject.factory() as db:
        target = await db.get(SyncManagedTarget, subject.iid)
        if changed == "installation_id":
            original = await db.get(SyncInstallation, subject.iid)
            other = SyncInstallation(
                id=uuid4(),
                workspace_id=subject.store.other,
                service_id=original.service_id,
                service_client_id=original.service_client_id,
                authorization_id=uuid4(),
                application_id="foreign-mode-binding",
                operations=original.operations,
                context_id=uuid4(),
                resource_pk=uuid4(),
                endpoint_id=uuid4(),
                resource_id="foreign-mode-binding",
                generation=1,
            )
            db.add(other)
            await db.flush()
            target.installation_id = other.id
        else:
            setattr(target, changed, 2 if changed == "generation" else uuid4())
        await db.commit()
    before = await footprint(subject)
    response = await human(subject, "GET", "management/access")
    assert response.status_code == 409, response.text
    private(response)
    assert await footprint(subject) == before
