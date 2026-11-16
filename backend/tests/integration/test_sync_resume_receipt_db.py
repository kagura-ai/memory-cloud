"""A fresh human can observe original outcomes without replaying publication."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select

from models.auth import Context, ContextMember, Workspace, WorkspaceMember
from models.memory import Memory
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from models.resource import ResourceEvent
from models.sync_activity import SyncManualRequest
from models.sync_credential import SyncAccessToken
from models.sync_feed import SyncFeed, SyncRecord, SyncRun
from models.sync_installation import SyncInstallation, SyncService
from models.sync_management import (
    SyncManagedTarget,
    SyncModeOperation,
    SyncResumeCandidate,
    SyncResumePreview,
)
from services import sync_management, sync_resume_receipt
from services.capacity_lock import ENTITLEMENT_SOURCE_EXTERNAL_BILLING, ensure_not_capacity_locked
from services.context_service import ContextService
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import human, product, request
from utils.exceptions import CapacityLockedError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, managed = (
    management.store,
    management.worker,
    management.feeds,
    management.managed,
)


def command(subject):
    return {
        "operation_id": str(uuid4()),
        "preview_id": str(uuid4()),
        "diff_hash": "a" * 64,
        "original_subject": subject.store.users["owner"],
    }


async def observe(subject, body, **kwargs):
    return await human(subject, "POST", "management/resume/receipt", body=body, **kwargs)


async def footprint(subject):
    """Compare every column of domain rows, including payloads and control receipts."""
    pairs = [
        (SyncInstallation, SyncInstallation.id == subject.iid),
        (SyncManagedTarget, SyncManagedTarget.installation_id == subject.iid),
        (SyncModeOperation, SyncModeOperation.installation_id == subject.iid),
        (SyncManualRequest, SyncManualRequest.installation_id == subject.iid),
        (SyncAccessToken, SyncAccessToken.installation_id == subject.iid),
        (SyncResumePreview, SyncResumePreview.installation_id == subject.iid),
        (SyncResumeCandidate, SyncResumeCandidate.installation_id == subject.iid),
        (SyncFeed, SyncFeed.installation_id == subject.iid),
        (Memory, Memory.context_id == UUID(subject.target["context_id"])),
        (ResourceEvent, ResourceEvent.resource_pk == UUID(subject.target["resource_pk"])),
        (Context, Context.id == UUID(subject.target["context_id"])),
    ]
    result = {}
    async with subject.factory() as db:
        for model, condition in pairs:
            rows = (await db.execute(select(*model.__table__.c).where(condition))).mappings()
            result[model.__tablename__] = sorted(
                json.dumps(dict(row), sort_keys=True, default=str) for row in rows
            )
        for model in (SyncRecord, SyncRun):
            rows = (
                await db.execute(
                    select(*model.__table__.c)
                    .join(SyncFeed, model.feed_id == SyncFeed.id)
                    .where(SyncFeed.installation_id == subject.iid)
                )
            ).mappings()
            result[model.__tablename__] = sorted(
                json.dumps(dict(row), sort_keys=True, default=str) for row in rows
            )
    return result


def outcome(response, subject, body, expected):
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    result = response.json()
    assert set(result) == {
        "contract_version",
        "workspace_id",
        "installation_id",
        "operation_id",
        "preview_id",
        "diff_hash",
        "original_subject",
        "result",
        "receipt",
        "as_of",
    }
    assert result["contract_version"] == "sync-resume-receipt-v1"
    assert result["workspace_id"] == str(subject.store.wid)
    assert result["installation_id"] == str(subject.iid)
    for key in body:
        assert result[key] == body[key]
    assert result["result"] == expected
    assert result["as_of"].endswith("Z")
    if expected != "found":
        assert result["receipt"] is None
    return result


async def actual_confirmation(subject):
    await management.manual(subject)
    await management.pin(subject, [product(title="Immutable resumed source")])
    preview = await human(subject, "POST", "management/resume/preview")
    assert preview.status_code == 200, preview.text
    p = preview.json()
    body = {
        "operation_id": str(uuid4()),
        "preview_id": p["preview_id"],
        "diff_hash": p["diff_hash"],
        "original_subject": subject.store.users["owner"],
    }
    response = await human(
        subject,
        "POST",
        "management/resume/confirm",
        body={k: v for k, v in body.items() if k != "original_subject"},
    )
    assert response.status_code == 200, response.text
    return body, response.json()


@pytest.mark.parametrize("role", ["owner", "member"])
async def test_absence_is_typed_and_does_not_enroll_or_allocate(managed, role):
    async with managed.factory() as db:
        await db.execute(
            delete(SyncManagedTarget).where(SyncManagedTarget.installation_id == managed.iid)
        )
        await db.commit()
    body = command(managed)
    before = await footprint(managed)
    outcome(await observe(managed, body, role=role), managed, body, "not_found")
    assert await footprint(managed) == before


async def test_actual_receipt_outlives_original_material_and_is_workspace_metadata(managed):
    body, receipt = await actual_confirmation(managed)
    # The confirming human's credentials are retired. A different current Editor
    # must not need or resurrect those credentials, preview or manual request.
    async with managed.factory() as db:
        token = await db.get(OIDCToken, managed.store.token_ids["owner"])
        grant = await db.get(OIDCGrant, managed.store.grant_ids["owner"])
        token.revoked_at = grant.revoked_at = managed.store.clock.now
        assert await db.get(SyncResumePreview, managed.iid) is None
        await db.commit()
    before = await footprint(managed)
    first = outcome(await observe(managed, body, role="member"), managed, body, "found")
    second = outcome(await observe(managed, body, role="member"), managed, body, "found")
    assert first["receipt"] == second["receipt"] == receipt
    assert await footprint(managed) == before


async def test_expired_then_deleted_original_credentials_do_not_prevent_fresh_editor_lookup(
    managed,
):
    body, receipt = await actual_confirmation(managed)
    async with managed.factory() as db:
        token = await db.get(OIDCToken, managed.store.token_ids["owner"])
        grant = await db.get(OIDCGrant, managed.store.grant_ids["owner"])
        token.issued_at -= 172800
        grant.created_at -= timedelta(days=2)
        grant.auth_time -= 172800
        grant.expires_at = managed.store.clock.now - timedelta(seconds=1)
        await db.commit()
    assert (
        outcome(await observe(managed, body, role="member"), managed, body, "found")["receipt"]
        == receipt
    )
    async with managed.factory() as db:
        await db.execute(delete(OIDCGrant).where(OIDCGrant.id == managed.store.grant_ids["owner"]))
        await db.commit()
    before = await footprint(managed)
    assert (
        outcome(await observe(managed, body, role="member"), managed, body, "found")["receipt"]
        == receipt
    )
    assert await footprint(managed) == before


async def test_live_original_preview_is_not_confirmed_by_lookup(managed):
    await management.manual(managed)
    await management.pin(managed, [product()])
    response = await human(managed, "POST", "management/resume/preview")
    assert response.status_code == 200, response.text
    p = response.json()
    body = {**command(managed), "preview_id": p["preview_id"], "diff_hash": p["diff_hash"]}
    before = await footprint(managed)
    outcome(await observe(managed, body), managed, body, "not_found")
    assert await footprint(managed) == before


@pytest.mark.parametrize("field", ["preview_id", "diff_hash", "original_subject", "action"])
async def test_reused_operation_reports_mismatch_without_unrelated_metadata(managed, field):
    body, _receipt = await actual_confirmation(managed)
    if field == "action":
        async with managed.factory() as db:
            row = await db.get(SyncModeOperation, (managed.iid, UUID(body["operation_id"])))
            row.action = "stop"
            await db.commit()
    else:
        body[field] = {
            "preview_id": str(uuid4()),
            "diff_hash": "f" * 64,
            "original_subject": "unrelated-request-subject",
        }[field]
    before = await footprint(managed)
    response = await observe(managed, body, role="member")
    outcome(response, managed, body, "mismatch")
    if field == "original_subject":
        assert managed.store.users["owner"] not in response.text
    assert "request_hash" not in response.text and "actor_user_id" not in response.text
    assert await footprint(managed) == before


@pytest.mark.parametrize("role", ["viewer", "outsider"])
async def test_current_editor_membership_required(managed, role):
    response = await observe(managed, command(managed), role=role)
    assert response.status_code == 404, response.text
    assert "as_of" not in response.text


@pytest.mark.parametrize(
    "change", ["token", "grant", "client", "registration", "scope", "role", "membership"]
)
async def test_current_authority_is_fresh_on_every_lookup(managed, change):
    body = command(managed)
    outcome(await observe(managed, body, role="member"), managed, body, "not_found")
    async with managed.factory() as db:
        if change == "registration":
            row = await db.get(SyncService, managed.store.service_id)
            row.browser_client_id = managed.store.foreign_client
        elif change == "scope":
            row = await db.get(OIDCToken, managed.store.token_ids["member"])
            row.scope = "openid workspaces:read"
        elif change == "role":
            row = await db.scalar(
                select(ContextMember).where(
                    ContextMember.context_id == UUID(managed.target["context_id"]),
                    ContextMember.user_id == managed.store.users["member"],
                )
            )
            row.role = "viewer"
        elif change == "membership":
            await db.execute(
                delete(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == managed.store.wid,
                    WorkspaceMember.user_id == managed.store.users["member"],
                )
            )
        else:
            model, key = {
                "token": (OIDCToken, managed.store.token_ids["member"]),
                "grant": (OIDCGrant, managed.store.grant_ids["member"]),
                "client": (OIDCClient, managed.store.client_id),
            }[change]
            row = await db.get(model, key)
            row.revoked_at = managed.store.clock.now
        await db.commit()
    response = await observe(managed, body, role="member")
    assert response.status_code in (401, 403, 404), response.text
    assert "as_of" not in response.text


@pytest.mark.parametrize("change", ["expired", "revoked", "capacity", "subscription"])
async def test_current_reach_keeps_metadata_after_installation_entitlement_loss(managed, change):
    body, receipt = await actual_confirmation(managed)
    if change == "subscription":
        await management.installations.paid(managed.store, revision=2, services=False)
    async with managed.factory() as db:
        installation = await db.get(SyncInstallation, managed.iid)
        if change == "expired":
            installation.expires_at = managed.store.clock.now - timedelta(seconds=1)
        elif change == "revoked":
            installation.revoked_at = managed.store.clock.now
        elif change == "capacity":
            workspace = await db.get(Workspace, managed.store.wid)
            workspace.plan_name = "free"
            workspace.entitlement_source = ENTITLEMENT_SOURCE_EXTERNAL_BILLING
            await db.flush()
            count = workspace.effective_memory_limit + 1
            assert 0 < count <= 20000
            await db.execute(
                insert(Memory),
                [
                    {
                        "user_id": managed.store.users["owner"],
                        "workspace_id": managed.store.wid,
                        "content": "Capacity fixture, not exposed by receipt metadata",
                        "summary": "Capacity fixture",
                        "type": "note",
                        "client": "pytest",
                    }
                    for _ in range(count)
                ],
            )
        await db.commit()
    before = await footprint(managed)
    if change == "capacity":
        async with managed.factory() as db:
            with pytest.raises(CapacityLockedError):
                await ensure_not_capacity_locked(db, managed.store.wid)
    assert outcome(await observe(managed, body), managed, body, "found")["receipt"] == receipt
    assert await footprint(managed) == before
    if change == "capacity":
        async with managed.factory() as db:
            await db.execute(
                delete(Memory).where(
                    Memory.workspace_id == managed.store.wid, Memory.context_id.is_(None)
                )
            )
            await db.commit()


@pytest.mark.parametrize("foreign", ["workspace", "installation", "worker"])
async def test_other_tenant_target_or_service_identity_cannot_observe(managed, foreign):
    body = command(managed)
    if foreign == "worker":
        response = await request(
            managed,
            "POST",
            f"/api/v1/workspaces/{managed.store.wid}/sync-installations/{managed.iid}/management/resume/receipt",
            token=managed.token,
            body=body,
        )
    else:
        response = await observe(
            managed,
            body,
            workspace=managed.store.other if foreign == "workspace" else None,
            installation=uuid4() if foreign == "installation" else None,
        )
    assert response.status_code in (401, 404), response.text
    assert "as_of" not in response.text


@pytest.mark.parametrize("extra", ["query", "body", "invalid", "large"])
async def test_request_is_bounded_strict_and_errors_do_not_reflect_input(managed, extra):
    body = command(managed)
    kwargs = {}
    marker = "never-echo-resume-receipt-input"
    if extra == "query":
        kwargs["params"] = {"scope": marker}
    elif extra == "body":
        body["authority"] = marker
    elif extra == "invalid":
        body["operation_id"] = marker
    else:
        body["original_subject"] = marker * 500
    response = await observe(managed, body, **kwargs)
    assert response.status_code == (413 if extra == "large" else 400), response.text
    assert marker not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("held", ["installation", "context"])
async def test_held_transition_never_becomes_successful_absence(managed, held):
    body = command(managed)
    async with managed.factory() as transition:
        model, key = (
            (SyncInstallation, managed.iid)
            if held == "installation"
            else (Context, UUID(managed.target["context_id"]))
        )
        await transition.scalar(select(model).where(model.id == key).with_for_update())
        response = await observe(managed, body)
        assert response.status_code == 409, response.text
        assert "not_found" not in response.text and "as_of" not in response.text
        await transition.rollback()
    outcome(await observe(managed, body), managed, body, "not_found")


async def test_owner_privacy_change_after_initial_rbac_cannot_return_a_stale_outcome(
    managed, monkeypatch
):
    await management.manual(managed)
    context_id = UUID(managed.target["context_id"])
    async with managed.factory() as owner:
        await ContextService(owner).update_context(
            managed.store.users["owner"], context_id, is_public=False
        )
    original = sync_resume_receipt.target_fence

    async def racing_privacy(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with managed.factory() as owner:
            await ContextService(owner).update_context(
                managed.store.users["owner"], context_id, is_private=True
            )
        return result

    monkeypatch.setattr(sync_resume_receipt, "target_fence", racing_privacy)
    response = await observe(managed, command(managed), role="admin")
    assert response.status_code == 404, response.text
    assert "as_of" not in response.text


async def test_retained_management_binding_drift_is_not_typed_absence(managed):
    async with managed.factory() as db:
        target = await db.get(SyncManagedTarget, managed.iid)
        target.generation += 1
        await db.commit()
    response = await observe(managed, command(managed))
    assert response.status_code == 409, response.text
    assert "as_of" not in response.text


async def test_expiry_during_observation_remains_a_commit_error(managed, monkeypatch):
    def expired_clock():
        now = managed.store.clock.now
        managed.store.clock.now += timedelta(days=2)
        return now

    monkeypatch.setattr(sync_resume_receipt, "utcnow", expired_clock)
    response = await observe(managed, command(managed))
    assert response.status_code == 409, response.text
    assert "as_of" not in response.text


async def test_disabled_feature_keeps_private_errors_without_a_receipt(managed, monkeypatch):
    monkeypatch.setattr(sync_management.get_settings(), "sync_managed_enabled", False)
    response = await observe(managed, command(managed))
    assert response.status_code == 404, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "as_of" not in response.text
