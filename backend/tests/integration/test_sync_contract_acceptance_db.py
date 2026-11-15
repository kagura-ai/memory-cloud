"""Cross-contract acceptance and restoration quarantine on durable SQL state (#2811)."""

import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.routes import sync_activity, well_known
from cli.sync_credentials import set_client_enabled
from cli.sync_service import set_service_enabled
from models.public_usage_budget import PublicUsageBudget
from models.resource import ResourceEvent
from models.sync_feed import SyncFeed, SyncRecord
from models.sync_installation import SyncInstallation
from services import sync_credentials
from services.public_usage_budget import charge_public_usage
from services.sync_contract import SyncContractUnsupported, require_sync_contract
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_credential_fixtures import mint
from tests.integration.sync_feed_fixtures import committed, human, service, staged
from tests.integration.test_alembic_migrations import _sync_engine

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds
FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/sync-contract-v1.json").read_text())


@pytest_asyncio.fixture(loop_scope="session")
async def target(feeds, monkeypatch):
    settings = well_known.get_settings()
    monkeypatch.setattr(settings, "sync_managed_enabled", True)
    monkeypatch.setattr(settings, "sync_observability_enabled", True)
    monkeypatch.setattr(settings, "mcp_public_usage_budget_enabled", True)
    feeds.store.app.include_router(well_known.router, prefix="/.well-known")
    feeds.store.app.include_router(sync_activity.router, prefix="/api/v1")
    return feeds


async def configuration(target):
    async with AsyncClient(
        transport=ASGITransport(app=target.store.app), base_url="https://test"
    ) as client:
        response = await client.get("/.well-known/kmc-sync-configuration")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def durable_state(target):
    async with target.factory() as db:
        installation = await db.get(SyncInstallation, target.iid)
        budget = await db.get(PublicUsageBudget, target.store.wid)
        return {
            "target": (installation.resource_pk, installation.context_id, installation.endpoint_id),
            "records": await db.scalar(
                select(func.count())
                .select_from(SyncRecord)
                .join(SyncFeed)
                .where(SyncFeed.installation_id == target.iid)
            ),
            "events": await db.scalar(
                select(func.count())
                .select_from(ResourceEvent)
                .where(ResourceEvent.resource_pk == installation.resource_pk)
            ),
            "budget": (budget.calls_used, budget.period_started_at, budget.period_seq),
        }


async def test_product_and_faq_share_target_and_private_control_reads_do_not_charge(target):
    assert require_sync_contract(await configuration(target))
    await committed(target, [FIXTURE["records"]["product"]])
    faq = copy.copy(target)
    faq.fid = uuid4()
    response = await human(
        faq, "PUT", f"feeds/{faq.fid}", body={"kind": "faq", "config_hash": "1" * 64}
    )
    assert response.status_code == 200, response.text
    await committed(faq, [FIXTURE["records"]["faq"]])
    async with target.factory() as db:
        await charge_public_usage(target.store.wid, limit=25_000, db=db, now=target.store.clock.now)
    before = await durable_state(target)
    projection = await human(target, "GET", "records")
    assert projection.status_code == 200, projection.text
    assert "Synthetic product" in projection.text and "Is this synthetic?" in projection.text
    for suffix in ("activity", "activity/runs", "activity/events"):
        response = await human(target, "GET", suffix)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
    assert await durable_state(target) == before
    assert before["records"] == 2 and before["budget"][0] == 1


async def test_recovery_quarantine_never_replays_old_tokens_or_prepared_runs(target):
    """Start from persisted pre-recovery rows, not process-local authority caches.

    This exercises the reconciliation stage after restoration, not a physical
    pg_dump/pg_restore operation. The operational runbook keeps restored services
    isolated until this quarantine and external entitlement reconciliation finish.
    """
    await committed(target, [FIXTURE["records"]["product"]])
    pending_record = copy.deepcopy(FIXTURE["records"]["product"])
    pending_record["fields"]["title"] = "Never replay this old queued snapshot"
    pending = await staged(target, [pending_record])
    async with target.factory() as db:
        await charge_public_usage(target.store.wid, limit=25_000, db=db, now=target.store.clock.now)
    before = await durable_state(target)
    old_token = target.token
    engine = _sync_engine()
    try:
        with Session(engine) as db, db.begin():
            set_service_enabled(db, target.store.service_id, False)
            set_client_enabled(db, target.client_id, False)
        with pytest.raises(sync_credentials.SyncCredentialError):
            await mint(target)
        assert (await service(target, "GET", "schema", token=old_token)).status_code == 401
        with Session(engine) as db, db.begin():
            set_client_enabled(db, target.client_id, True)
            set_service_enabled(db, target.store.service_id, True)
    finally:
        engine.dispose()
    # A new process/credential still cannot authorize the old generation's job.
    target.token = (await mint(target))["access_token"]
    assert (await service(target, "GET", "schema", token=old_token)).status_code == 401
    replay = await service(
        target, "POST", f"runs/{pending['run_id']}/commit", body={"diff_hash": pending["diff_hash"]}
    )
    assert replay.status_code == 409, replay.text
    assert await durable_state(target) == before
    fresh_record = copy.deepcopy(FIXTURE["records"]["product"])
    fresh_record["fields"]["title"] = "New snapshot after reconciliation"
    assert (await committed(target, [fresh_record]))["state"] == "committed"
    after = await durable_state(target)
    assert after["target"] == before["target"] and after["budget"] == before["budget"]
    assert after["records"] == before["records"] == 1
    assert after["events"] > before["events"]


async def test_rollout_disable_is_a_consumer_stop_even_with_a_live_old_token(target, monkeypatch):
    assert require_sync_contract(await configuration(target))
    monkeypatch.setattr(well_known.get_settings(), "sync_runs_enabled", False)
    with pytest.raises(SyncContractUnsupported):
        require_sync_contract(await configuration(target))
    assert (await service(target, "GET", "schema")).status_code == 404
