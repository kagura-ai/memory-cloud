"""Prepared approval must not revive a lost original manual authority."""

from datetime import UTC, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from models.auth import AuditLog, ContextMember, Workspace
from models.oidc import OIDCGrant, OIDCToken
from models.sync_activity import SyncManualRequest
from models.sync_feed import SyncRun
from tests.integration import test_sync_activity_db as fixtures
from tests.integration.sync_feed_fixtures import committed, human, product, staged
from tests.integration.sync_installation_fixtures import change
from utils.datetime import to_utc_iso

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, activity = fixtures.store, fixtures.worker, fixtures.feeds, fixtures.activity


async def prepared_manual(activity, *, prepare_delay=timedelta()):
    await committed(activity, [product()])
    response = await human(
        activity, "POST", "activity/requests", role="member", body={"operation_id": str(uuid4())}
    )
    assert response.status_code == 201, response.text
    proof = response.json()
    activity.store.clock.now += prepare_delay
    row = await staged(activity, [], manual_request_id=proof["request_id"])
    assert row["requires_approval"] is True
    return proof, row


async def shorten_login(activity, remaining: timedelta):
    """End the requester's login (its OIDC grant) ``remaining`` from now; return the end."""
    async with activity.factory() as db:
        grant = await db.get(OIDCGrant, activity.store.grant_ids["member"])
        grant.expires_at = (max(grant.created_at, activity.store.clock.now) + remaining).replace(
            microsecond=0
        ) + timedelta(seconds=1)
        await db.commit()
        return grant.expires_at


async def test_expired_access_token_alone_keeps_the_original_request(activity):
    """#2911: the stored access token may expire; the login behind it still holds."""
    _proof, row = await prepared_manual(activity)
    await change(
        activity.store,
        OIDCToken,
        activity.store.token_ids["member"],
        issued_at=int(
            (activity.store.clock.now.replace(tzinfo=UTC) - timedelta(seconds=20)).timestamp()
        ),
        expires_in=1,
    )
    response = await approve(activity, row)
    assert response.status_code == 200, response.text


async def approve(activity, row):
    return await human(
        activity, "POST", f"runs/{row['run_id']}/approvals", body={"diff_hash": row["diff_hash"]}
    )


async def assert_unapproved(activity, row):
    async with activity.factory() as db:
        current = await db.get(SyncRun, UUID(row["run_id"]))
        assert current.state == "prepared"
        assert current.diff_hash == row["diff_hash"]
        assert current.approval_id is None and current.approved_by is None
        assert current.approval_epoch is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "sync.diff.approved",
                    AuditLog.user_metadata["run_id"].as_string() == row["run_id"],
                )
            )
            == 0
        )


async def approver_denials(activity):
    async with activity.factory() as db:
        return await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "sync.authority.denied",
                AuditLog.user_id == activity.store.users["owner"],
            )
        )


def assert_refused(response):
    assert response.status_code == 409, response.text
    assert response.json() == {"error": "RES-002"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "mutation",
    [
        "revoked",
        "grant",
        "login_expired",
        "scope",
        "grant_scope",
        "role",
        "seat",
        "owner_epoch",
        "other_source",
    ],
)
async def test_manual_approval_rechecks_original_provenance_even_for_another_valid_editor(
    activity, mutation
):
    _proof, row = await prepared_manual(activity)
    denials_before = await approver_denials(activity)
    if mutation == "seat":
        # Seats shrink below the member count: the original requester keeps the
        # membership row and Editor role but no longer has a seat; the owner does.
        async with activity.factory() as db:
            workspace = await db.get(Workspace, activity.store.wid)
            workspace.plan_name, workspace.addon_member_bonus = "basic", 0
            await db.commit()
    elif mutation == "revoked":
        await change(
            activity.store,
            OIDCToken,
            activity.store.token_ids["member"],
            revoked_at=activity.store.clock.now,
        )
    elif mutation == "grant":
        await change(
            activity.store,
            OIDCGrant,
            activity.store.grant_ids["member"],
            revoked_at=activity.store.clock.now,
        )
    elif mutation == "login_expired":
        # The request is bound to the login (#2911): its grant ends first.
        login_ends = await shorten_login(activity, timedelta(seconds=30))
        activity.store.clock.now = login_ends
    elif mutation == "scope":
        await change(
            activity.store,
            OIDCToken,
            activity.store.token_ids["member"],
            scope="openid workspaces:read",
        )
    elif mutation == "grant_scope":
        await change(
            activity.store,
            OIDCGrant,
            activity.store.grant_ids["member"],
            scope="openid workspaces:read",
        )
    elif mutation == "role":
        async with activity.factory() as db:
            member = await db.scalar(
                select(ContextMember).where(
                    ContextMember.context_id == UUID(activity.target["context_id"]),
                    ContextMember.user_id == activity.store.users["member"],
                )
            )
            member.role = "viewer"
            await db.commit()
    elif mutation == "owner_epoch":
        async with activity.factory() as db:
            workspace = await db.get(Workspace, activity.store.wid)
            workspace.ownership_epoch += 1
            await db.commit()
    else:
        response = await human(
            activity, "PUT", f"feeds/{uuid4()}", body={"kind": "faq", "config_hash": "c" * 64}
        )
        assert response.status_code == 200, response.text
    response = await approve(activity, row)
    assert_refused(response)
    await assert_unapproved(activity, row)
    # A lost original proof is a conflict, never a denial of the current approver.
    assert await approver_denials(activity) == denials_before


async def test_live_manual_approval_preserves_original_request_and_deadlines_across_different_editors(
    activity,
):
    proof, row = await prepared_manual(activity)
    response = await approve(activity, row)
    assert response.status_code == 200, response.text
    original = response.json()
    repeated = await approve(activity, row)
    assert repeated.status_code == 200 and repeated.json() == original
    assert original["expires_at"] == row["prepared_until"]
    async with activity.factory() as db:
        request = await db.get(SyncManualRequest, UUID(proof["request_id"]))
        current = await db.get(SyncRun, UUID(row["run_id"]))
        assert request.requested_by == activity.store.users["member"]
        assert current.approved_by == activity.store.users["owner"]
        assert current.manifest["manual_request_id"] == proof["request_id"]
        assert to_utc_iso(current.expires_at) == row["expires_at"]
        assert to_utc_iso(request.expires_at) == proof["expires_at"]


async def test_automatic_approval_still_works_without_manual_provenance(activity):
    await committed(activity, [product()])
    row = await staged(activity, [])
    assert row["requires_approval"] is True
    response = await approve(activity, row)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("reference", ["", False, {}])
async def test_malformed_declared_manual_provenance_never_becomes_automatic_approval(
    activity, reference
):
    _proof, row = await prepared_manual(activity)
    async with activity.factory() as db:
        current = await db.get(SyncRun, UUID(row["run_id"]))
        current.manifest = {**current.manifest, "manual_request_id": reference}
        await db.commit()
    response = await approve(activity, row)
    assert_refused(response)
    await assert_unapproved(activity, row)


async def test_original_login_deadline_is_rechecked_when_approval_commits(activity, monkeypatch):
    from services import sync_audit

    _proof, row = await prepared_manual(activity)
    # The original login ends while the approval is being written (#2911).
    login_ends = await shorten_login(activity, timedelta(seconds=2))
    original = sync_audit.add_audit

    def expire(db, action, actor, **kwargs):
        result = original(db, action, actor, **kwargs)
        if action == "sync.diff.approved":
            activity.store.clock.now = login_ends
        return result

    monkeypatch.setattr(sync_audit, "add_audit", expire)
    response = await approve(activity, row)
    assert_refused(response)
    await assert_unapproved(activity, row)


async def test_original_request_expiry_is_independent_of_live_token_and_prepared_deadline(activity):
    proof, row = await prepared_manual(activity, prepare_delay=timedelta(minutes=2))
    assert proof["expires_at"] < row["prepared_until"]
    activity.store.clock.now += timedelta(minutes=14)
    assert proof["expires_at"] < to_utc_iso(activity.store.clock.now) < row["prepared_until"]
    response = await approve(activity, row)
    assert_refused(response)
    await assert_unapproved(activity, row)


async def test_original_request_deadline_is_fenced_during_approval_with_live_original_token(
    activity, monkeypatch
):
    from services import sync_audit

    _proof, row = await prepared_manual(activity, prepare_delay=timedelta(minutes=2))
    activity.store.clock.now += timedelta(minutes=12, seconds=59)
    original = sync_audit.add_audit

    def expire(db, action, actor, **kwargs):
        result = original(db, action, actor, **kwargs)
        if action == "sync.diff.approved":
            activity.store.clock.now += timedelta(seconds=2)
        return result

    monkeypatch.setattr(sync_audit, "add_audit", expire)
    response = await approve(activity, row)
    assert_refused(response)
    await assert_unapproved(activity, row)
