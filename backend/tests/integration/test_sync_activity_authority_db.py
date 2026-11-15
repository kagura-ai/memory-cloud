"""Real current-authority, replay, erasure and truthful search evidence regressions."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from models.auth import AuditLog, ContextMember, User, Workspace
from models.memory import Memory
from models.oidc import OIDCToken
from models.sync_activity import SyncManualRequest, SyncManualRun
from models.sync_feed import SyncRun
from services import sync_audit, sync_feed_commit
from tests.integration import test_sync_activity_db as fixtures
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import (
    committed,
    human,
    manifest,
    product,
    service,
    staged,
)
from tests.integration.sync_installation_fixtures import change, paid
from tests.integration.test_sync_management_db import apply_pending

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, activity = fixtures.store, fixtures.worker, fixtures.feeds, fixtures.activity
managed = management.managed


async def request(activity, *, role="member", operation=None):
    response = await human(
        activity,
        "POST",
        "activity/requests",
        role=role,
        body={"operation_id": str(operation or uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_replays_do_not_extend_consent_or_duplicate_request_and_commit_audit(activity):
    operation = uuid4()
    original = await request(activity, operation=operation)
    again = await request(activity, operation=operation)
    assert again == original
    records = [product()]
    body = manifest(activity, records, manual_request_id=original["request_id"])
    response = await service(activity, "POST", "runs", body=body)
    assert response.status_code == 201
    row = response.json()
    retry = await service(activity, "POST", "runs", body=body)
    assert retry.json()["run_id"] == row["run_id"]
    changed = await service(activity, "POST", "runs", body={**body, "operation_id": str(uuid4())})
    assert changed.status_code == 409
    async with activity.factory() as db:
        for action in ("sync.request.created", "sync.run.accepted"):
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.resource == "sync:" + str(activity.iid),
                        AuditLog.action == action,
                    )
                )
                == 1
            )


@pytest.mark.parametrize(
    "mutation", ["scope", "role", "token", "owner_epoch", "generation", "config"]
)
async def test_queued_manual_work_requires_current_human_and_source_authority(activity, mutation):
    from models.sync_installation import SyncInstallation

    proof = await request(activity)
    if mutation == "token":
        await change(
            activity.store,
            OIDCToken,
            activity.store.token_ids["member"],
            revoked_at=activity.store.clock.now,
        )
    elif mutation == "scope":
        await change(
            activity.store,
            OIDCToken,
            activity.store.token_ids["member"],
            scope="openid workspaces:read",
        )
    elif mutation == "owner_epoch":
        async with activity.factory() as db:
            row = await db.get(Workspace, activity.store.wid)
            row.ownership_epoch += 1
            await db.commit()
    elif mutation == "generation":
        async with activity.factory() as db:
            row = await db.get(SyncInstallation, activity.iid)
            row.generation += 1
            await db.commit()
        from tests.integration.sync_credential_fixtures import mint

        activity.token = (await mint(activity))["access_token"]
    elif mutation == "config":
        response = await human(
            activity,
            "PUT",
            f"feeds/{activity.fid}",
            body={"kind": "product", "config_hash": "4" * 64},
        )
        assert response.status_code == 200
    else:
        async with activity.factory() as db:
            member = await db.scalar(
                select(ContextMember).where(
                    ContextMember.context_id == UUID(activity.target["context_id"]),
                    ContextMember.user_id == activity.store.users["member"],
                )
            )
            member.role = "viewer"
            await db.commit()
    body = manifest(activity, [product()], manual_request_id=proof["request_id"])
    if mutation == "config":
        body.update(config_hash="4" * 64, config_version=2)
    response = await service(activity, "POST", "runs", body=body)
    assert response.status_code in {401, 403, 404, 409}, response.text
    async with activity.factory() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(SyncRun).where(SyncRun.feed_id == activity.fid)
            )
            == 0
        )


async def test_manual_expiry_after_last_await_rolls_back_commit_and_its_audit(
    activity, monkeypatch
):
    proof = await request(activity)
    row = await staged(activity, [product()], manual_request_id=proof["request_id"])
    await change(
        activity.store,
        SyncManualRequest,
        UUID(proof["request_id"]),
        expires_at=activity.store.clock.now + timedelta(seconds=2),
    )
    original = sync_feed_commit.clear_staging

    async def expire(db, run):
        await original(db, run)
        activity.store.clock.now += timedelta(seconds=3)

    monkeypatch.setattr(sync_feed_commit, "clear_staging", expire)
    response = await service(
        activity, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert response.status_code == 409, response.text
    async with activity.factory() as db:
        assert (await db.get(SyncRun, UUID(row["run_id"]))).state == "prepared"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.resource == "sync:" + str(activity.iid),
                    AuditLog.action == "sync.run.committed",
                )
            )
            == 0
        )


async def test_caller_headers_and_extra_json_cannot_name_a_manual_actor(activity):
    path = f"/api/v1/workspaces/{activity.store.wid}/sync-installations/{activity.iid}/activity/requests"
    forged = "secret-forged-actor-value"
    headers = {
        "Authorization": "Bearer " + activity.store.tokens["member"],
        "X-Actor-Sub": forged,
        "X-User-Id": forged,
        "X-Requested-By": forged,
    }
    async with AsyncClient(
        transport=ASGITransport(app=activity.store.app), base_url="https://test"
    ) as client:
        response = await client.post(path, headers=headers, json={"operation_id": str(uuid4())})
        assert response.status_code == 201
        denied = await client.post(
            path, headers=headers, json={"operation_id": str(uuid4()), "requested_by": forged}
        )
        assert denied.status_code == 400
        assert forged not in denied.text
    async with activity.factory() as db:
        rows = (
            await db.scalars(
                select(AuditLog).where(AuditLog.resource == "sync:" + str(activity.iid))
            )
        ).all()
        assert any(
            row.action == "sync.request.created" and row.user_id == activity.store.users["member"]
            for row in rows
        )
        assert all(forged not in json.dumps(row.user_metadata) for row in rows)


async def test_unknown_manual_request_does_not_grant_attribution_or_create_a_run(activity):
    response = await service(
        activity,
        "POST",
        "runs",
        body=manifest(activity, [product()], manual_request_id=str(uuid4())),
    )
    assert response.status_code == 409


async def test_live_request_limit_keeps_existing_replays_available(activity):
    operation = uuid4()
    first = await request(activity, operation=operation)
    for _ in range(3):
        await request(activity)
    denied = await human(
        activity, "POST", "activity/requests", role="member", body={"operation_id": str(uuid4())}
    )
    assert denied.status_code == 409
    assert await request(activity, operation=operation) == first


async def test_denial_audit_outage_never_changes_refusal_or_reflects_exception_text(
    activity, monkeypatch
):
    async with activity.factory() as db:
        await db.execute(
            delete(ContextMember).where(ContextMember.user_id == activity.store.users["member"])
        )
        await db.commit()

    def unavailable(_db):
        raise RuntimeError("private-exception-body-and-token")

    monkeypatch.setattr(sync_audit, "isolated_session", unavailable)
    denied = await human(activity, "GET", "activity", role="member")
    assert denied.status_code == 404
    assert "private-exception-body-and-token" not in denied.text


async def test_allowance_and_notification_key_are_passive_and_shared(activity, monkeypatch):
    from models.public_usage_budget import PublicUsageBudget
    from services.public_usage_budget import get_settings

    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_enabled", True)
    monkeypatch.setattr(get_settings(), "mcp_public_usage_budget_calls", 25000)
    async with activity.factory() as db:
        db.add(
            PublicUsageBudget(
                workspace_id=activity.store.wid,
                period_started_at=activity.store.clock.now - timedelta(minutes=1),
                calls_used=22500,
                period_seq=7,
                updated_at=activity.store.clock.now,
            )
        )
        await db.commit()
    first = await human(activity, "GET", "activity")
    second = await human(activity, "GET", "activity", role="member")
    assert first.status_code == second.status_code == 200
    assert first.json()["allowance"]["remaining"] == 2500
    assert first.json()["notification"] == second.json()["notification"]
    assert first.json()["notification"]["reached"] == 90
    assert first.json()["allowance"]["resets_at"].endswith("Z")
    async with activity.factory() as db:
        assert (await db.get(PublicUsageBudget, activity.store.wid)).calls_used == 22500


async def test_read_checks_identity_expiry_after_slow_snapshot_work(activity, monkeypatch):
    from services import sync_activity

    original = sync_activity.read_public_usage_budget

    async def expire(db, wid):
        result = await original(db, wid)
        activity.store.clock.now += timedelta(hours=2)
        return result

    monkeypatch.setattr(sync_activity, "read_public_usage_budget", expire)
    response = await human(activity, "GET", "activity")
    assert response.status_code == 409
    assert "record_count" not in response.text


async def test_receipt_pagination_is_stable_and_cannot_change_target_scope(activity):
    expected = {(await committed(activity, []))["run_id"] for _ in range(3)}
    seen, cursor = set(), None
    while True:
        response = await human(
            activity,
            "GET",
            "activity/runs",
            params={"limit": 1, **({"cursor": cursor} if cursor else {})},
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert all(item["search"]["state"] == "not_evaluated" for item in page["items"])
        ids = {item["run_id"] for item in page["items"]}
        assert not ids.intersection(seen)
        seen.update(ids)
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == expected
    unknown = await human(activity, "GET", "activity/runs", params={"cursor": str(uuid4())})
    assert unknown.status_code == 404


async def test_resume_audits_the_actual_human_commit_and_drain_state(activity, managed):
    await management.manual(managed)
    candidate = await management.pin(managed, [product()])
    preview = await human(activity, "POST", "management/resume/preview")
    assert preview.status_code == 200, preview.text
    confirmed = await human(
        activity,
        "POST",
        "management/resume/confirm",
        body={
            "operation_id": str(uuid4()),
            "preview_id": preview.json()["preview_id"],
            "diff_hash": preview.json()["diff_hash"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    response = await human(activity, "GET", "activity/events")
    assert response.status_code == 200, response.text
    events = response.json()["items"]
    commits = [event for event in events if event["action"] == "sync.run.committed"]
    assert len(commits) == 1
    assert commits[0]["actor"]["kind"] == "human"
    assert commits[0]["run_id"] == candidate["candidate_id"]
    assert commits[0]["correlation_id"] is not None
    manual = next(event for event in events if event["action"] == "sync.management.manual")
    assert manual["mode"] == "manual" and manual["management_epoch"] > 1
    assert manual["stop_watermark"] is not None


async def test_member_diagnostics_survive_paid_expiry_but_current_visibility_is_required(activity):
    await paid(activity.store, 2, services=False)
    result = await human(activity, "GET", "activity", role="member")
    assert result.status_code == 200, result.text
    async with activity.factory() as db:
        await db.execute(
            delete(ContextMember).where(
                ContextMember.context_id == UUID(activity.target["context_id"]),
                ContextMember.user_id == activity.store.users["member"],
            )
        )
        await db.commit()
    denied = await human(activity, "GET", "activity", role="member")
    assert denied.status_code == 404
    async with activity.factory() as db:
        denial = await db.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == "sync.authority.denied",
                AuditLog.resource == "sync:" + str(activity.iid),
                AuditLog.user_id == activity.store.users["member"],
            )
            .order_by(AuditLog.id.desc())
        )
        assert denial is not None
        assert denial.user_metadata["reason"] == "not_found"


async def test_noop_commit_waits_for_prior_data_and_detects_same_version_corruption(
    activity, monkeypatch
):
    await committed(activity, [product()])
    latest = await committed(activity, [product()])
    pending = await human(activity, "GET", f"activity/runs/{latest['run_id']}")
    assert pending.json()["search"]["state"] == "pending"
    await apply_pending(activity, monkeypatch)
    complete = await human(activity, "GET", f"activity/runs/{latest['run_id']}")
    assert complete.json()["search"]["state"] == "unchanged", complete.text
    async with activity.factory() as db:
        row = await db.scalar(
            select(Memory).where(Memory.context_id == UUID(activity.target["context_id"]))
        )
        row.content = '{"title":"private-corrupted-content"}'
        await db.commit()
    failed = await human(activity, "GET", f"activity/runs/{latest['run_id']}")
    assert failed.json()["search"]["state"] == "failed"
    assert "private-corrupted-content" not in failed.text


async def test_erasure_breaks_human_provenance_and_retention_preserves_source_receipts(activity):
    from services.erasure_details import pseudonymize_audit_logs

    proof = await request(activity)
    row = await committed(activity, [product()], manual_request_id=proof["request_id"])
    async with activity.factory() as db:
        await pseudonymize_audit_logs(db, activity.store.users["member"])
        await db.execute(delete(User).where(User.user_id == activity.store.users["member"]))
        await db.commit()
    response = await human(activity, "GET", "activity/events")
    assert response.status_code == 200
    assert activity.store.users["member"] not in response.text
    async with activity.factory() as db:
        request_row = await db.get(SyncManualRequest, UUID(proof["request_id"]))
        assert request_row.requested_by is None and request_row.token_id is None
        request_row.created_at = activity.store.clock.now - timedelta(days=91)
        request_row.expires_at = request_row.created_at + timedelta(minutes=5)
        expired_audit = await db.scalar(
            select(AuditLog).where(
                AuditLog.resource == "sync:" + str(activity.iid),
                AuditLog.action == "sync.run.prepared",
            )
        )
        expired_audit.created_at = request_row.created_at
        expired_audit_id = expired_audit.id
        old = AuditLog(
            user_id=activity.store.users["owner"],
            user_email="system:legacy",
            action="legacy.audit",
            resource="legacy",
            user_metadata={"untouched": True},
            created_at=request_row.created_at,
        )
        db.add(old)
        await db.flush()
        legacy_id = old.id
        await sync_audit.prune_activity(db)
        await db.commit()
        assert await db.get(SyncManualRequest, UUID(proof["request_id"])) is None
        assert await db.get(AuditLog, legacy_id) is not None
        assert await db.get(AuditLog, expired_audit_id) is None
        run = await db.get(SyncRun, UUID(row["run_id"]))
        await db.refresh(run)
        assert (await db.get(SyncManualRun, UUID(row["run_id"]))).request_id is None
        assert run.state == "committed" and run.manifest["manual_request_id"] == proof["request_id"]


async def test_retention_skips_busy_audit_rows_and_keeps_each_tick_bounded(activity):
    async with activity.factory() as db:
        rows = [
            AuditLog(
                user_id=activity.store.users["owner"],
                user_email="system:sync-audit",
                action="sync.authority.denied",
                resource="sync:" + str(activity.iid),
                user_metadata={"schema": sync_audit.SCHEMA},
                created_at=activity.store.clock.now - timedelta(days=91),
            )
            for _ in range(3)
        ]
        db.add_all(rows)
        await db.commit()
        ids = [row.id for row in rows]
    async with activity.factory() as busy, activity.factory() as cleanup:
        await busy.scalar(select(AuditLog).where(AuditLog.id == ids[0]).with_for_update())
        await sync_audit.prune_activity(cleanup, limit=1)
        await cleanup.commit()
        remaining = set(
            (await cleanup.scalars(select(AuditLog.id).where(AuditLog.id.in_(ids)))).all()
        )
        assert len(remaining) == 2 and ids[0] in remaining
        await busy.rollback()
