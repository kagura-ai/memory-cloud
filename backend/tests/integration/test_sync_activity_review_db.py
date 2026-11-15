"""Regression tests for independent review: lifecycle races and bounded projections."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import delete, event, inspect, select
from sqlalchemy.orm import Session

from models.auth import AuditLog, ContextMember, User
from models.memory import Memory
from models.resource import IndexerState
from models.sync_activity import SyncManualRequest
from models.sync_feed import SyncRecord, SyncRun
from services import sync_audit, sync_resume
from tests.integration import test_sync_activity_db as fixtures
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import committed, human, product, service, staged
from tests.integration.test_alembic_migrations import _sync_engine

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, activity = fixtures.store, fixtures.worker, fixtures.feeds, fixtures.activity
managed = management.managed


@pytest.mark.parametrize("reregister", [False, True])
async def test_denial_after_erasure_never_reinserts_raw_subject_even_after_reregistration(
    activity, monkeypatch, reregister
):
    from services.erasure_details import pseudonymize_audit_logs

    subject = activity.store.users["member"]
    async with activity.factory() as db:
        await db.execute(delete(ContextMember).where(ContextMember.user_id == subject))
        await db.commit()
    original = sync_audit.record_denial

    async def erase_after_rollback(status):
        async with activity.factory() as db:
            await pseudonymize_audit_logs(db, subject)
            await db.execute(delete(User).where(User.user_id == subject))
            await db.flush()
            if reregister:
                db.add(User(user_id=subject, email=uuid4().hex + "@example.test", role="user"))
            await db.commit()
        await original(status)

    monkeypatch.setattr(sync_audit, "record_denial", erase_after_rollback)
    response = await human(activity, "GET", "activity", role="member")
    assert response.status_code == 404
    async with activity.factory() as db:
        assert (
            await db.scalar(select(AuditLog.id).where(AuditLog.user_id == subject).limit(1)) is None
        )


@pytest.mark.parametrize("mutation", ["expired", "token", "role", "erased"])
async def test_pinned_manual_request_is_rechecked_before_resume_publication(
    activity, managed, mutation
):
    from tests.integration.test_sync_activity_authority_db import request

    await management.manual(managed)
    await management.mint(managed)
    proof = await request(activity)
    row = await staged(activity, [product()], manual_request_id=proof["request_id"])
    pin = await service(activity, "POST", "resume-candidates", body={"run_id": row["run_id"]})
    assert pin.status_code == 200, pin.text
    preview = await human(activity, "POST", "management/resume/preview")
    assert preview.status_code == 200, preview.text
    async with activity.factory() as db:
        if mutation == "expired":
            proof_row = await db.get(SyncManualRequest, UUID(proof["request_id"]))
            proof_row.expires_at = proof_row.created_at + timedelta(seconds=1)
            activity.store.clock.now += timedelta(seconds=2)
        elif mutation == "token":
            from models.oidc import OIDCToken

            token = await db.get(OIDCToken, activity.store.token_ids["member"])
            token.revoked_at = activity.store.clock.now
        elif mutation == "erased":
            from services.erasure_details import pseudonymize_audit_logs

            await pseudonymize_audit_logs(db, activity.store.users["member"])
            await db.execute(delete(User).where(User.user_id == activity.store.users["member"]))
        else:
            grant = await db.scalar(
                select(ContextMember).where(
                    ContextMember.user_id == activity.store.users["member"],
                    ContextMember.context_id == UUID(activity.target["context_id"]),
                )
            )
            grant.role = "viewer"
        await db.commit()
    result = await human(
        activity,
        "POST",
        "management/resume/confirm",
        body={
            "operation_id": str(uuid4()),
            "preview_id": preview.json()["preview_id"],
            "diff_hash": preview.json()["diff_hash"],
        },
    )
    assert result.status_code in {401, 403, 404, 409}, result.text
    async with activity.factory() as db:
        assert (await db.get(SyncRun, UUID(row["run_id"]))).state == "aborted"
        assert (
            await db.scalar(
                select(SyncRecord.id).where(SyncRecord.feed_id == activity.fid).limit(1)
            )
            is None
        )


async def test_manual_candidate_deadline_is_held_through_resume_final_commit(
    activity, managed, monkeypatch
):
    from tests.integration.test_sync_activity_authority_db import request

    await management.manual(managed)
    await management.mint(managed)
    proof = await request(activity)
    row = await staged(activity, [product()], manual_request_id=proof["request_id"])
    pin = await service(activity, "POST", "resume-candidates", body={"run_id": row["run_id"]})
    assert pin.status_code == 200
    preview = await human(activity, "POST", "management/resume/preview")
    async with activity.factory() as db:
        proof_row = await db.get(SyncManualRequest, UUID(proof["request_id"]))
        proof_row.expires_at = activity.store.clock.now + timedelta(seconds=2)
        await db.commit()
    original = sync_resume.publish_resumed_feed

    async def expire(db, installation, feed, candidate):
        await original(db, installation, feed, candidate)
        activity.store.clock.now += timedelta(seconds=3)

    monkeypatch.setattr(sync_resume, "publish_resumed_feed", expire)
    result = await human(
        activity,
        "POST",
        "management/resume/confirm",
        body={
            "operation_id": str(uuid4()),
            "preview_id": preview.json()["preview_id"],
            "diff_hash": preview.json()["diff_hash"],
        },
    )
    assert result.status_code == 409, result.text
    async with activity.factory() as db:
        assert (await db.get(SyncRun, UUID(row["run_id"]))).state == "aborted"
        assert (
            await db.scalar(
                select(SyncRecord.id).where(SyncRecord.feed_id == activity.fid).limit(1)
            )
            is None
        )


async def test_receipt_pages_and_cursor_never_load_large_diff_json(activity):
    rows = [await committed(activity, []) for _ in range(2)]
    async with activity.factory() as db:
        for item in rows:
            run = await db.get(SyncRun, UUID(item["run_id"]))
            run.diff = [
                {"op": "upsert", "external_id": "private-marker-" + str(i)} for i in range(20000)
            ]
        await db.commit()
    loaded = []

    def metadata_only(row, _context):
        assert "diff" in inspect(row).unloaded, (
            "large diff JSON was deserialized into the metadata request"
        )
        loaded.append(row.id)

    event.listen(SyncRun, "load", metadata_only)
    try:
        first = await human(activity, "GET", "activity/runs", params={"limit": 1})
        assert first.status_code == 200, first.text
        assert first.json()["items"][0]["changes"] == 20000
        second = await human(
            activity,
            "GET",
            "activity/runs",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
        )
        assert second.status_code == 200, second.text
        detail = await human(activity, "GET", f"activity/runs/{rows[-1]['run_id']}")
        assert detail.status_code == 200, detail.text
        assert "private-marker" not in first.text + second.text + detail.text
        assert loaded
    finally:
        event.remove(SyncRun, "load", metadata_only)


async def test_other_feed_failures_do_not_override_current_canonical_search_proof(
    activity, monkeypatch
):
    fid = activity.fid
    faq_id = uuid4()
    configured = await human(
        activity, "PUT", f"feeds/{faq_id}", body={"kind": "faq", "config_hash": "1" * 64}
    )
    assert configured.status_code == 200
    activity.fid = faq_id
    faq_run = await committed(
        activity,
        [
            {
                "external_id": "q",
                "fields": {"question": "Question", "answer": "Private-other-feed-answer"},
            }
        ],
    )
    activity.fid = fid
    latest = await committed(activity, [product()])
    await management.apply_pending(activity, monkeypatch)
    async with activity.factory() as db:
        faq = await db.scalar(select(SyncRecord).where(SyncRecord.feed_id == faq_id))
        memory = await db.scalar(
            select(Memory).where(
                Memory.context_id == UUID(activity.target["context_id"]),
                Memory.resource_doc_id == faq.doc_id,
            )
        )
        memory.embedding_status = "failed"
        state = await db.scalar(
            select(IndexerState).where(
                IndexerState.context_id == UUID(activity.target["context_id"])
            )
        )
        state.metrics = {
            "failed_events": [{"event_id": faq.last_event_id, "error": "private-other-feed-error"}]
        }
        await db.commit()
    response = await human(activity, "GET", f"activity/runs/{latest['run_id']}")
    assert response.status_code == 200
    assert response.json()["search"]["state"] == "complete"
    assert "private-other-feed-error" not in response.text
    failed = await human(activity, "GET", f"activity/runs/{faq_run['run_id']}")
    assert failed.json()["search"]["state"] == "failed"
    assert "Private-other-feed-answer" not in failed.text


async def test_worker_operations_name_the_key_used_after_rotation_and_service_generation_changes(
    activity,
):
    from cli import sync_credentials as keys
    from cli import sync_service as services
    from tests.integration.sync_credential_fixtures import mint

    initial = await committed(activity, [product()])
    second_key = RSAKey.generate_key(2048, parameters={"kid": "audit-next", "alg": "RS256"})
    engine = _sync_engine()
    try:
        with Session(engine) as db, db.begin():
            second = keys.add_key(db, activity.client_id, second_key.as_dict(private=False))
            second_id = second.id
        activity.token = (await mint(activity, key=second_key, kid="audit-next"))["access_token"]
        latest = await committed(activity, [product()])
        response = await human(activity, "GET", "activity/events")
        assert response.status_code == 200, response.text
        commits = {
            item["run_id"]: item
            for item in response.json()["items"]
            if item["action"] == "sync.run.committed"
        }
        assert commits[initial["run_id"]]["key_id"] == str(activity.key_id)
        assert commits[latest["run_id"]]["key_id"] == str(second_id)
        with Session(engine) as db, db.begin():
            services.set_service_enabled(db, activity.store.service_id, False)
            services.set_service_enabled(db, activity.store.service_id, False)
            services.set_service_enabled(db, activity.store.service_id, True)
        response = await human(activity, "GET", "activity/events")
        transitions = [
            item for item in response.json()["items"] if item["action"].startswith("sync.service.")
        ]
        assert sorted(item["action"] for item in transitions) == [
            "sync.service.disabled",
            "sync.service.enabled",
        ]
        assert sorted(item["generation"] for item in transitions) == [2, 3]
    finally:
        engine.dispose()
