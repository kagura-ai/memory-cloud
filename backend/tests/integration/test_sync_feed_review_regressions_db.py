"""Concrete gate-2 regressions: ABA consent, staging bounds and full diff pages."""

from datetime import timedelta
from uuid import UUID

import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import func, select

from auth.sync_service_jwt import key_fingerprint, public_key
from models.sync_credential import SyncSigningKey
from models.sync_feed import SyncChunk, SyncRun
from services import sync_feed_commit
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_credential_fixtures import mint
from tests.integration.sync_feed_fixtures import (
    committed,
    human,
    manifest,
    product,
    service,
    staged,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


async def test_configuration_aba_does_not_revive_old_destructive_approval(feeds):
    await committed(feeds, [product()])
    row = await staged(feeds, [])
    body = {"diff_hash": row["diff_hash"]}
    approval = (await human(feeds, "POST", f"runs/{row['run_id']}/approvals", body=body)).json()
    for fingerprint in ("3" * 64, "1" * 64):
        result = await human(
            feeds, "PUT", f"feeds/{feeds.fid}", body={"kind": "product", "config_hash": fingerprint}
        )
        assert result.status_code == 200
    assert result.json()["config_version"] == 3
    result = await service(
        feeds,
        "POST",
        f"runs/{row['run_id']}/commit",
        body={**body, "approval_id": approval["approval_id"]},
    )
    assert result.status_code == 409
    assert (await human(feeds, "GET", "records")).json()["records"]


async def test_declared_record_count_is_enforced_before_staging(feeds):
    body = manifest(feeds, [product("one"), product("two")], chunks=2)
    rid = (await service(feeds, "POST", "runs", body=body)).json()["run_id"]
    assert (
        await service(
            feeds, "PUT", f"runs/{rid}/chunks/0", body={"records": [product("one"), product("two")]}
        )
    ).status_code == 200
    result = await service(
        feeds, "PUT", f"runs/{rid}/chunks/1", body={"records": [product("third")]}
    )
    assert result.status_code == 400 and result.json()["error"] == "too_many_records"
    async with feeds.factory() as db:
        assert (
            await db.scalar(
                select(func.sum(SyncChunk.record_count)).where(SyncChunk.run_id == UUID(rid))
            )
            == 2
        )


async def test_all_twenty_thousand_diff_entries_can_be_paged(feeds):
    await committed(feeds, [product(f"old-{index:05}") for index in range(10000)])
    row = await staged(feeds, [product(f"new-{index:05}") for index in range(10000)])
    assert row["changes"] == 20000 and row["requires_approval"] is True
    for request in (
        lambda suffix: service(feeds, "GET", suffix),
        lambda suffix: human(feeds, "GET", suffix),
    ):
        result = await request(f"runs/{row['run_id']}/diff?offset=19900&limit=100")
        assert result.status_code == 200, result.text
        assert len(result.json()["diff"]) == 100 and result.json()["next_offset"] is None


async def test_identical_commit_retry_is_valid_after_optional_human_approval(feeds):
    row = await staged(feeds, [product()])
    body = {"diff_hash": row["diff_hash"]}
    assert (
        await human(feeds, "POST", f"runs/{row['run_id']}/approvals", body=body)
    ).status_code == 200
    for _attempt in range(2):
        result = await service(feeds, "POST", f"runs/{row['run_id']}/commit", body=body)
        assert result.status_code == 200, result.text


async def test_prepared_run_survives_real_key_rotation_and_retirement(feeds):
    row = await staged(feeds, [product()])
    key = RSAKey.generate_key(2048, parameters={"kid": "next", "alg": "RS256"})
    document = public_key(key.as_dict(private=False))
    async with feeds.factory() as db:
        db.add(
            SyncSigningKey(
                client_id=feeds.client_id,
                kid="next",
                public_jwk=document,
                fingerprint=key_fingerprint(document),
            )
        )
        (await db.get(SyncSigningKey, feeds.key_id)).revoked_at = feeds.store.clock.now
        await db.commit()
    assert (await service(feeds, "GET", f"runs/{row['run_id']}")).status_code == 401
    feeds.token = (await mint(feeds, key=key, kid="next"))["access_token"]
    result = await service(
        feeds, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert result.status_code == 200 and result.json()["state"] == "committed"


async def test_preparation_expiry_after_final_await_rolls_back_with_fresh_token(feeds, monkeypatch):
    row = await staged(feeds, [product()])
    feeds.store.clock.now += timedelta(seconds=890)
    feeds.token = (await mint(feeds))["access_token"]
    original = sync_feed_commit.clear_staging

    async def expire_after_flush(db, record):
        await original(db, record)
        feeds.store.clock.now += timedelta(seconds=10)

    monkeypatch.setattr(sync_feed_commit, "clear_staging", expire_after_flush)
    result = await service(
        feeds, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert result.status_code == 409
    async with feeds.factory() as db:
        assert (await db.get(SyncRun, UUID(row["run_id"]))).state == "prepared"


async def test_stream_limits_duplicate_keys_and_unknown_selectors_are_fixed_errors(feeds):
    from httpx import ASGITransport, AsyncClient

    base = f"/api/v1/sync/installations/{feeds.iid}/runs"
    async with AsyncClient(
        transport=ASGITransport(app=feeds.store.app), base_url="https://test"
    ) as client:
        headers = {"Authorization": "Bearer " + feeds.token, "Content-Type": "application/json"}
        for content, status in [
            (b"x" * 8193, 413),
            (b'{"records":1,"records":2}', 400),
            (b"not-json", 400),
        ]:
            response = await client.post(base, content=content, headers=headers)
            assert (
                response.status_code == status and response.headers["cache-control"] == "no-store"
            )
            assert "x" * 100 not in response.text
    assert (await human(feeds, "GET", "records?limit=101")).status_code == 400
    assert (await human(feeds, "GET", "records?limit=1&limit=2")).status_code == 400
    assert (await human(feeds, "GET", "records?resource_id=foreign")).status_code == 400
