"""A manual request lasts 15 minutes of its login, not one 300 s access token (#2911).

The identity server issues every access token for at most 300 seconds, and a
refresh writes a new token row. A manual request is bound to the login (the
OIDC grant) behind the token that made it: it lasts at most 15 minutes and
never beyond that login, an expired or rotated access token alone does not end
it, and anything that revokes the login ends it at once. A run whose request
is no longer current gives back its active-run slot and is expired with its
staged records cleared.

These tests mint realistic 300-second tokens for the requester and approver
(the shared fixtures' 3600 s tokens would hide the token lifetime).
"""

import json
import secrets
from datetime import UTC, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import bcrypt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from joserfc.jwk import RSAKey
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from api.routes import oidc
from auth import sync_identity
from auth.oidc_server import credential_hash
from config.settings import get_settings
from models.oidc import OIDCAuthorizationCode, OIDCClient, OIDCGrant, OIDCToken
from models.sync_feed import SyncChunk, SyncRun
from services import sync_requests
from services.oauth_grant_revocation import revoke_oauth_grants
from services.sync_feed_index import maintain_sync_runs
from tests.integration import sync_credential_fixtures as credentials
from tests.integration import test_sync_activity_db as fixtures
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import (
    committed,
    human,
    manifest,
    product,
    request,
    service,
    staged,
)
from tests.integration.sync_installation_fixtures import change
from tests.integration.test_alembic_migrations import _sync_engine
from tests.integration.test_sync_manual_requester_seat_db import assert_conflict, conflicts_of
from utils.datetime import to_utc_iso
from utils.exceptions import ConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, activity = fixtures.store, fixtures.worker, fixtures.feeds, fixtures.activity
managed = management.managed

ACCESS_TOKEN_SECONDS = 300  # what the identity server issues (min(300, grant remaining))
SECRET = "lifetime-2911-client-secret"
CHANGED = "Manual request authority changed"
ENDED = "Manual request is no longer current"


async def login(activity, role="member", *, seconds=ACCESS_TOKEN_SECONDS) -> SimpleNamespace:
    """A fresh access/refresh pair of ``role``'s login, as a sign-in or a refresh issues it."""
    access = "kmc_oidc_at_" + secrets.token_urlsafe(32)
    refresh = "kmc_oidc_rt_" + secrets.token_urlsafe(32)
    async with activity.factory() as db:
        grant = await db.get(OIDCGrant, activity.store.grant_ids[role])
        row = OIDCToken(
            grant_id=grant.id,
            access_token_hash=credential_hash(access),
            refresh_token_hash=credential_hash(refresh),
            scope=grant.scope,
            issued_at=int(activity.store.clock.now.replace(tzinfo=UTC).timestamp()),
            expires_in=seconds,
        )
        db.add(row)
        await db.commit()
    return SimpleNamespace(access=access, refresh=refresh, token_id=row.id)


async def as_login(activity, token, method, suffix, *, body=None, **kwargs):
    return await request(
        activity,
        method,
        f"/api/v1/workspaces/{activity.store.wid}/sync-installations/{activity.iid}/{suffix}",
        token=token.access,
        body=body,
        **kwargs,
    )


async def manual_request(activity, token) -> dict:
    response = await as_login(
        activity, token, "POST", "activity/requests", body={"operation_id": str(uuid4())}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def later(activity, **delta) -> None:
    """Let time pass; the worker's own 300 s service token is renewed as a worker would."""
    activity.store.clock.now += timedelta(**delta)
    activity.token = (await credentials.mint(activity))["access_token"]


async def create(activity, records=None, **extra):
    return await service(
        activity, "POST", "runs", body=manifest(activity, records or [product()], **extra)
    )


async def stage(activity, run_id, records=None):
    return await stage_index(activity, run_id, 0, records)


async def stage_index(activity, run_id, index, records=None):
    return await service(
        activity,
        "PUT",
        f"runs/{run_id}/chunks/{index}",
        body={"records": records or [product()]},
    )


@pytest_asyncio.fixture(loop_scope="session")
async def identity(activity, tmp_path, monkeypatch):
    """The real identity endpoints (/oidc/token, /oidc/revoke) on the same database."""
    key = RSAKey.generate_key(2048)
    private = key.as_dict(private=True)
    private.update(kid="test-rsa", alg="RS256", use="sig")
    key_file = tmp_path / "signing.json"
    key_file.write_text(json.dumps({"keys": [private]}), encoding="utf-8")
    key_file.chmod(0o600)
    settings = get_settings()
    monkeypatch.setattr(settings, "oidc_signing_keys_file", str(key_file))
    monkeypatch.setattr(settings, "oidc_active_signing_kid", "test-rsa")
    engine = _sync_engine()
    monkeypatch.setattr(oidc, "get_sync_session", sessionmaker(engine, expire_on_commit=False))

    async def within_rate_limit(_request, _presented=None):
        return None

    monkeypatch.setattr(oidc, "check_oidc_rate_limit", within_rate_limit)
    async with activity.factory() as db:
        client = await db.get(OIDCClient, activity.store.client_id)
        client.client_secret_hash = bcrypt.hashpw(SECRET.encode(), bcrypt.gensalt(4)).decode()
        await db.commit()
    activity.store.app.include_router(oidc.router, prefix="/oidc")
    try:
        yield activity
    finally:
        engine.dispose()


async def identity_post(activity, path, data):
    async with AsyncClient(
        transport=ASGITransport(app=activity.store.app), base_url="https://test"
    ) as client:
        return await client.post(
            "/oidc/" + path,
            data={"client_id": activity.store.client_id, "client_secret": SECRET, **data},
        )


async def assert_login_revoked(activity) -> None:
    async with activity.factory() as db:
        grant = await db.get(OIDCGrant, activity.store.grant_ids["member"])
        assert grant is None or grant.revoked_at is not None


# --- the login recheck ----------------------------------------------------------


async def expire_token(store, role="member") -> None:
    past = int((store.clock.now.replace(tzinfo=UTC) - timedelta(seconds=20)).timestamp())
    await change(store, OIDCToken, store.token_ids[role], issued_at=past, expires_in=1)


async def test_login_identity_outlives_the_access_token_but_not_the_grant(store):
    await expire_token(store)
    async with store.factory() as db:
        with pytest.raises(HTTPException) as refused:
            await sync_identity.load_sync_identity(
                db, credential_hash(store.tokens["member"]), "integrations:manage"
            )
        assert refused.value.status_code == 401  # unchanged for every other caller
        live = await sync_identity.load_login_identity(
            db, store.token_ids["member"], "integrations:manage", lock=True
        )
        login_ends = (await db.get(OIDCGrant, store.grant_ids["member"])).expires_at
        assert (live.user_id, live.client_id) == (store.users["member"], store.client_id)
        assert live.expires_at == login_ends
        await db.rollback()
    store.clock.now = login_ends
    async with store.factory() as db:
        with pytest.raises(HTTPException) as ended:
            await sync_identity.load_login_identity(
                db, store.token_ids["member"], "integrations:manage"
            )
        assert ended.value.status_code == 401


@pytest.mark.parametrize(
    "model, field, value, status",
    [
        (OIDCGrant, "revoked_at", "now", 401),
        (OIDCClient, "revoked_at", "now", 401),
        (OIDCToken, "revoked_at", "now", 401),
        (OIDCGrant, "scope", "openid workspaces:read", 403),
        (OIDCClient, "scope", "openid workspaces:read", 403),
    ],
    ids=["grant-revoked", "client-revoked", "token-row-revoked", "grant-scope", "client-scope"],
)
async def test_login_identity_refuses_a_revoked_or_narrowed_login(
    store, model, field, value, status
):
    key = {
        OIDCGrant: store.grant_ids["member"],
        OIDCClient: store.client_id,
        OIDCToken: store.token_ids["member"],
    }[model]
    await change(store, model, key, **{field: store.clock.now if value == "now" else value})
    async with store.factory() as db:
        with pytest.raises(HTTPException) as refused:
            await sync_identity.load_login_identity(
                db, store.token_ids["member"], "integrations:manage", lock=True
            )
    assert refused.value.status_code == status


async def test_login_recheck_and_a_revocation_exclude_each_other(store):
    """The recheck holds the grant shared until its transaction ends; a revocation
    in flight makes the recheck refuse at once (retryable) instead of waiting."""
    grant_id, token_id = store.grant_ids["member"], store.token_ids["member"]
    revoke = update(OIDCGrant).where(OIDCGrant.id == grant_id).values(revoked_at=store.clock.now)
    async with store.factory() as checker, store.factory() as revoker:
        await sync_identity.load_login_identity(checker, token_id, "integrations:manage", lock=True)
        await revoker.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError):
            await revoker.execute(revoke)
        await revoker.rollback()
        await checker.rollback()
    async with store.factory() as revoker, store.factory() as checker:
        await revoker.execute(revoke)
        with pytest.raises(ConflictError) as busy:
            await sync_identity.load_login_identity(
                checker, token_id, "integrations:manage", lock=True
            )
        assert busy.value.message == "Identity authority is changing; retry"
        await checker.rollback()
        await revoker.commit()
    async with store.factory() as checker:
        with pytest.raises(HTTPException) as revoked:
            await sync_identity.load_login_identity(
                checker, token_id, "integrations:manage", lock=True
            )
        assert revoked.value.status_code == 401


# --- lifetime -----------------------------------------------------------------


async def test_manual_request_outlives_its_300_second_token_for_create_stage_approval_and_commit(
    activity,
):
    # A baseline of two records: a snapshot of one deletes half, which needs approval.
    await committed(activity, [product("one"), product("two")])
    made_at = activity.store.clock.now
    token = await login(activity)
    proof = await manual_request(activity, token)
    assert proof["expires_at"] == to_utc_iso(made_at + timedelta(minutes=15))

    # Six minutes later the requester's access token has expired.
    await later(activity, minutes=6)
    created = await create(activity, [product("one")], manual_request_id=proof["request_id"])
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    staged_chunk = await stage(activity, run_id, [product("one")])
    assert staged_chunk.status_code == 200, staged_chunk.text
    prepared = await service(activity, "POST", f"runs/{run_id}/prepare")
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["requires_approval"] is True

    # About ten minutes after the request, an Editor signed in at that time approves.
    await later(activity, minutes=4)
    approver = await login(activity, "owner")
    approval = await as_login(
        activity,
        approver,
        "POST",
        f"runs/{run_id}/approvals",
        body={"diff_hash": prepared.json()["diff_hash"]},
    )
    assert approval.status_code == 200, approval.text

    await later(activity, minutes=1)
    result = await service(
        activity,
        "POST",
        f"runs/{run_id}/commit",
        body={
            "diff_hash": prepared.json()["diff_hash"],
            "approval_id": approval.json()["approval_id"],
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "committed"


async def test_manual_request_ends_15_minutes_after_it_was_made(activity, monkeypatch):
    token = await login(activity)
    proof = await manual_request(activity, token)
    await later(activity, minutes=6)
    created = await create(activity, manual_request_id=proof["request_id"])
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]

    await later(activity, minutes=8, seconds=59)
    staged_chunk = await stage(activity, run_id)
    assert staged_chunk.status_code == 200, staged_chunk.text

    await later(activity, seconds=1)
    seen = conflicts_of(monkeypatch, sync_requests, "validate_manual_request")
    assert_conflict(await service(activity, "POST", f"runs/{run_id}/prepare"))
    assert seen == [ENDED]


async def test_manual_request_never_outlives_its_login(activity, monkeypatch):
    async with activity.factory() as db:
        grant = await db.get(OIDCGrant, activity.store.grant_ids["member"])
        login_ends = max(grant.created_at, activity.store.clock.now) + timedelta(minutes=4)
        grant.expires_at = login_ends.replace(microsecond=0)
        login_ends = grant.expires_at
        await db.commit()
    token = await login(activity)
    proof = await manual_request(activity, token)
    assert proof["expires_at"] == to_utc_iso(login_ends)

    await later(activity, minutes=3)
    created = await create(activity, manual_request_id=proof["request_id"])
    assert created.status_code == 201, created.text

    activity.store.clock.now = login_ends
    activity.token = (await credentials.mint(activity))["access_token"]
    seen = conflicts_of(monkeypatch, sync_requests, "validate_manual_request")
    assert_conflict(await stage(activity, created.json()["run_id"]))
    assert seen == [ENDED]


@pytest.mark.parametrize("minutes, current", [(10, True), (15, False)])
async def test_resume_confirmation_follows_the_15_minutes_of_a_300_second_login(
    activity, managed, monkeypatch, minutes, current
):
    await management.manual(managed)
    token = await login(activity)
    proof = await manual_request(activity, token)
    await later(activity, minutes=6)
    row = await staged(managed, [product()], manual_request_id=proof["request_id"])
    pinned = await service(managed, "POST", "resume-candidates", body={"run_id": row["run_id"]})
    assert pinned.status_code == 200, pinned.text

    await later(activity, minutes=2)
    preview = await human(activity, "POST", "management/resume/preview")
    assert preview.status_code == 200, preview.text
    diff = await human(
        activity,
        "GET",
        "management/resume/diff",
        params={"preview_id": preview.json()["preview_id"]},
    )
    assert diff.status_code == 200, diff.text

    await later(activity, minutes=minutes - 8)
    seen = conflicts_of(monkeypatch, sync_requests, "validate_manual_request")
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
    if current:
        assert confirmed.status_code == 200, confirmed.text
        assert seen == []
    else:
        assert_conflict(confirmed)
        assert seen == [ENDED]


# --- the active-run cap ------------------------------------------------------


async def assert_run_expired_and_cleared(activity, run_id) -> None:
    """The run is expired and none of its staged chunks keeps a body."""
    async with activity.factory() as db:
        assert (await db.get(SyncRun, UUID(str(run_id)))).state == "expired"
        chunks = (
            await db.scalars(select(SyncChunk).where(SyncChunk.run_id == UUID(str(run_id))))
        ).all()
        assert chunks
        assert [chunk.records for chunk in chunks] == [None] * len(chunks)


async def test_ended_manual_runs_give_back_their_active_run_slots(activity):
    runs = []
    for _ in range(4):
        proof = await manual_request(activity, await login(activity))
        created = await create(activity, manual_request_id=proof["request_id"])
        assert created.status_code == 201, created.text
        runs.append(UUID(created.json()["run_id"]))
        staged_chunk = await stage(activity, runs[-1])
        assert staged_chunk.status_code == 200, staged_chunk.text

    # The access tokens have expired, the requests have not: the slots stay held.
    await later(activity, minutes=6)
    refused = await create(activity)
    assert_conflict(refused)
    async with activity.factory() as db:
        assert {(await db.get(SyncRun, rid)).state for rid in runs} == {"uploading"}

    await later(activity, minutes=9)
    accepted = await create(activity)
    assert accepted.status_code == 201, accepted.text
    # The ended runs were expired and their staged records cleared, so staging
    # stays bounded by the four active runs.
    for rid in runs:
        await assert_run_expired_and_cleared(activity, rid)


async def test_maintenance_expires_a_run_whose_login_was_revoked_before_24_hours(activity):
    ended_proof = await manual_request(activity, await login(activity))
    ended = await create(activity, manual_request_id=ended_proof["request_id"])
    assert ended.status_code == 201, ended.text
    assert (await stage(activity, ended.json()["run_id"])).status_code == 200
    automatic = await create(activity)
    assert automatic.status_code == 201, automatic.text
    assert (await stage(activity, automatic.json()["run_id"])).status_code == 200

    await change(
        activity.store,
        OIDCGrant,
        activity.store.grant_ids["member"],
        revoked_at=activity.store.clock.now,
    )
    async with activity.factory() as db:
        await maintain_sync_runs(db)
        await db.commit()

    await assert_run_expired_and_cleared(activity, ended.json()["run_id"])
    async with activity.factory() as db:
        kept = await db.get(SyncRun, UUID(automatic.json()["run_id"]))
        assert kept.state == "uploading"
        chunk = await db.get(SyncChunk, (kept.id, 0))
        assert chunk.records is not None


# --- revocation ends the request at once ---------------------------------------


async def sign_out(activity, token) -> None:
    response = await identity_post(activity, "revoke", {"token": token.refresh})
    assert response.status_code == 200, response.text


async def refresh_reuse(activity, token) -> None:
    rotated = await identity_post(
        activity, "token", {"grant_type": "refresh_token", "refresh_token": token.refresh}
    )
    assert rotated.status_code == 200, rotated.text
    reused = await identity_post(
        activity, "token", {"grant_type": "refresh_token", "refresh_token": token.refresh}
    )
    assert reused.status_code == 400, reused.text
    assert reused.json()["error"] == "invalid_grant"


async def code_replay(activity, token) -> None:
    code = "kmc_oidc_code_" + secrets.token_urlsafe(32)
    async with activity.factory() as db:
        db.add(
            OIDCAuthorizationCode(
                code_hash=credential_hash(code),
                grant_id=activity.store.grant_ids["member"],
                redirect_uri="https://client.example.test/callback",
                code_challenge="c" * 43,
                code_challenge_method="S256",
                expires_at=activity.store.clock.now + timedelta(seconds=60),
                consumed_at=activity.store.clock.now,
            )
        )
        await db.commit()
    replay = await identity_post(
        activity,
        "token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://client.example.test/callback",
            "code_verifier": secrets.token_urlsafe(48),
        },
    )
    assert replay.status_code == 400, replay.text
    assert replay.json()["error"] == "invalid_grant"


async def password_reset(activity, token) -> None:
    async with activity.factory() as db:
        await revoke_oauth_grants(db, activity.store.users["member"])
        await db.commit()


async def erasure(activity, token) -> None:
    # The grant step of account erasure deletes the login; its token rows go with it.
    async with activity.factory() as db:
        await revoke_oauth_grants(db, activity.store.users["member"], delete_tokens=True)
        await db.commit()


REVOCATIONS = {
    "sign_out": (sign_out, CHANGED),
    "refresh_reuse": (refresh_reuse, CHANGED),
    "code_replay": (code_replay, CHANGED),
    "password_reset": (password_reset, CHANGED),
    "erasure": (erasure, ENDED),
}


@pytest.mark.parametrize("path", list(REVOCATIONS))
async def test_revoking_the_login_ends_the_request_and_frees_its_slot_at_once(
    identity, monkeypatch, path
):
    activity = identity
    revoke, message = REVOCATIONS[path]
    token = await login(activity)
    proof = await manual_request(activity, token)
    pair = [product("first"), product("second")]
    manual_run = await create(activity, pair, chunks=2, manual_request_id=proof["request_id"])
    assert manual_run.status_code == 201, manual_run.text
    run_id = manual_run.json()["run_id"]
    first = await stage_index(activity, run_id, 0, [pair[0]])
    assert first.status_code == 200, first.text
    for _ in range(3):
        automatic = await create(activity)
        assert automatic.status_code == 201, automatic.text
    assert_conflict(await create(activity))

    await revoke(activity, token)
    await assert_login_revoked(activity)

    seen = conflicts_of(monkeypatch, sync_requests, "validate_manual_request")
    assert_conflict(await stage_index(activity, run_id, 1, [pair[1]]))
    assert seen == [message]
    # No clock advance: the run gives back its slot as soon as the login is revoked,
    # and its staged records are cleared with it.
    accepted = await create(activity)
    assert accepted.status_code == 201, accepted.text
    await assert_run_expired_and_cleared(activity, run_id)


async def test_rotating_the_requesters_access_token_keeps_the_request(identity):
    activity = identity
    token = await login(activity)
    proof = await manual_request(activity, token)
    rotated = await identity_post(
        activity, "token", {"grant_type": "refresh_token", "refresh_token": token.refresh}
    )
    assert rotated.status_code == 200, rotated.text
    await later(activity, minutes=6)
    created = await create(activity, manual_request_id=proof["request_id"])
    assert created.status_code == 201, created.text


async def test_revoking_the_identity_client_ends_the_request_and_frees_its_slot(
    activity, monkeypatch
):
    token = await login(activity)
    proof = await manual_request(activity, token)
    pair = [product("first"), product("second")]
    created = await create(activity, pair, chunks=2, manual_request_id=proof["request_id"])
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    first = await stage_index(activity, run_id, 0, [pair[0]])
    assert first.status_code == 200, first.text
    for _ in range(3):
        automatic = await create(activity)
        assert automatic.status_code == 201, automatic.text
    assert_conflict(await create(activity))
    await change(
        activity.store, OIDCClient, activity.store.client_id, revoked_at=activity.store.clock.now
    )
    seen = conflicts_of(monkeypatch, sync_requests, "validate_manual_request")
    assert_conflict(await stage_index(activity, run_id, 1, [pair[1]]))
    assert seen == [CHANGED]
    accepted = await create(activity)
    assert accepted.status_code == 201, accepted.text
    await assert_run_expired_and_cleared(activity, run_id)
