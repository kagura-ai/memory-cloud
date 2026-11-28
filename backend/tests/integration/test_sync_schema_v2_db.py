"""Record schema version 2 on a real installation (#3055).

- A version-1 installation, or one without a stored schema, refuses ``page``
  and ``review`` at feed configuration with the bytes of an unknown kind and
  writes nothing.
- A refused chunk names the first refused record, the field and the reason.
- An installation that stores version 2 answers ``/schema`` with the version-2
  bytes, accepts the version-2 kinds and fields and refuses the version-1 hash.
- ``SYNC_SCHEMA_V2_ENABLED``: a first configuration stores version 2; the
  upgrade operation, one test per state (switch off, upgraded then repeated,
  stale snapshot, a viewer, active, dead and aborted runs, no or an
  unregistered schema, resume candidates and preview, a records cursor and a
  manual request binding); turning the switch off keeps a version-2
  installation working.

Not executed in the local unit run: needs the DB container.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update

from config.settings import get_settings
from models.auth import AuditLog
from models.resource import ResourceSchema
from models.sync_feed import SyncFeed, SyncRun
from models.sync_feed_schemas import SCHEMA_V1, SCHEMA_V2
from models.sync_installation import SyncInstallation
from models.sync_management import SyncResumeCandidate, SyncResumePreview
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_feed_fixtures import committed, human, manifest, product, service

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds

FIXTURES = Path(__file__).parents[1] / "fixtures"
INVALID_REQUEST = b'{"error":"invalid_request"}'


@pytest_asyncio.fixture(loop_scope="session")
async def observed(feeds, monkeypatch):
    from api.routes import sync_activity as routes
    from services import sync_activity

    monkeypatch.setattr(sync_activity.get_settings(), "sync_observability_enabled", True)
    monkeypatch.setattr(sync_activity, "utcnow", lambda: feeds.store.clock.now)
    feeds.store.app.include_router(routes.router, prefix="/api/v1")
    return feeds


async def audits(worker) -> int:
    async with worker.factory() as db:
        return (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.user_metadata["installation_id"].as_string() == str(worker.iid))
            )
        ) or 0


async def schema_rows(worker) -> list[int]:
    async with worker.factory() as db:
        installation = await db.get(SyncInstallation, worker.iid)
        return list(
            (
                await db.scalars(
                    select(ResourceSchema.schema_version)
                    .where(ResourceSchema.resource_pk == installation.resource_pk)
                    .order_by(ResourceSchema.schema_version)
                )
            ).all()
        )


async def store_version_two(worker) -> None:
    """Append the version-2 schema row, as the upgrade operation does."""
    async with worker.factory() as db:
        installation = await db.get(SyncInstallation, worker.iid)
        db.add(
            ResourceSchema(
                resource_pk=installation.resource_pk,
                resource_id=installation.resource_id,
                schema_version=2,
                field_definitions=SCHEMA_V2.stored_definitions(),
            )
        )
        await db.commit()


async def feed_count(worker) -> int:
    async with worker.factory() as db:
        return (
            await db.scalar(
                select(func.count())
                .select_from(SyncFeed)
                .where(SyncFeed.installation_id == worker.iid)
            )
        ) or 0


async def configure(worker, kind: str, fid: UUID | None = None, **extra):
    return await human(
        worker,
        "PUT",
        f"feeds/{fid or uuid4()}",
        body={"kind": kind, "config_hash": "1" * 64, **extra},
    )


async def test_a_version_one_installation_refuses_the_new_kinds_like_an_unknown_kind(feeds):
    before = (await audits(feeds), await feed_count(feeds), await schema_rows(feeds))
    unknown = await configure(feeds, "blog")
    assert (unknown.status_code, unknown.content) == (400, INVALID_REQUEST)
    for kind in ("page", "review"):
        for extra in (
            {},
            {
                "expected": {
                    "config_version": 0,
                    "generation": 1,
                    "schema_hash": "0" * 64,
                    "schema_version": 9,
                }
            },
        ):
            response = await configure(feeds, kind, **extra)
            assert response.status_code == 400, response.text
            assert response.content == unknown.content
            assert response.headers["cache-control"] == unknown.headers["cache-control"]
    assert (await audits(feeds), await feed_count(feeds), await schema_rows(feeds)) == before
    assert before[2] == [1]


async def test_an_installation_without_a_schema_refuses_them_and_stores_nothing(feeds):
    async with feeds.factory() as db:
        installation = await db.get(SyncInstallation, feeds.iid)
        await db.execute(delete(SyncFeed).where(SyncFeed.installation_id == feeds.iid))
        await db.execute(
            delete(ResourceSchema).where(ResourceSchema.resource_pk == installation.resource_pk)
        )
        await db.commit()
    before = await audits(feeds)
    for kind in ("page", "review"):
        response = await configure(feeds, kind)
        assert (response.status_code, response.content) == (400, INVALID_REQUEST)
    assert await schema_rows(feeds) == []
    assert await audits(feeds) == before and await feed_count(feeds) == 0
    first = await configure(feeds, "product", feeds.fid)
    assert first.status_code == 200, first.text
    assert (first.json()["schema_version"], first.json()["schema_hash"]) == (
        1,
        SCHEMA_V1.schema_hash,
    )
    assert await schema_rows(feeds) == [1]


async def chunk(worker, records, **extra):
    run = await service(worker, "POST", "runs", body=manifest(worker, records, chunks=1, **extra))
    assert run.status_code == 201, run.text
    return await service(
        worker, "PUT", f"runs/{run.json()['run_id']}/chunks/0", body={"records": records}
    )


@pytest.mark.parametrize(
    ("second", "detail"),
    [
        (
            product("two", url="http://insecure.example/item"),
            b'"record":1,"field":"url","reason":"invalid_url"',
        ),
        (product("two", color="red"), b'"record":1,"reason":"unknown_field"'),
        (product("two", question="Q"), b'"record":1,"field":"question","reason":"unknown_field"'),
        (
            product("two", price="12"),
            b'"record":1,"field":"currency","reason":"requires_field"',
        ),
        (
            product("two", description="\u3042" * 10_900),
            b'"record":1,"reason":"record_too_large"',
        ),
    ],
)
async def test_a_refused_chunk_names_record_field_and_reason(feeds, second, detail):
    response = await chunk(feeds, [product("one"), second])
    assert response.status_code == 400
    assert response.content == b'{"error":"invalid_record",' + detail + b"}"


async def test_a_version_two_installation_serves_its_schema_kinds_and_fields(observed):
    worker = observed
    await store_version_two(worker)
    schema = await service(worker, "GET", "schema")
    assert schema.status_code == 200, schema.text
    assert schema.content == (FIXTURES / "sync-schema-v2.json").read_bytes().removesuffix(b"\n")

    page_fid = uuid4()
    configured = await configure(worker, "page", page_fid)
    assert configured.status_code == 200, configured.text
    assert (configured.json()["schema_version"], configured.json()["schema_hash"]) == (
        2,
        SCHEMA_V2.schema_hash,
    )
    v2 = {"schema_version": 2, "schema_hash": SCHEMA_V2.schema_hash}
    page = {
        "external_id": "about-1",
        "fields": {
            "title": "About",
            "url": "https://brand.example.test/about",
            "body": "We make shoes.\nSince 1990.",
            "heading": "History",
        },
    }
    # The version-1 pair no longer names this installation's schema.
    stale = await service(
        worker, "POST", "runs", body=manifest(worker, [page], feed_id=str(page_fid))
    )
    assert (stale.status_code, stale.content) == (400, b'{"error":"invalid_manifest"}')
    stale_version = await service(
        worker,
        "POST",
        "runs",
        body=manifest(worker, [page], feed_id=str(page_fid), schema_hash=SCHEMA_V2.schema_hash),
    )
    assert (stale_version.status_code, stale_version.content) == (409, b'{"error":"RES-002"}')
    await committed(worker, [page], feed_id=str(page_fid), **v2)

    rich = product(
        "one",
        price="20",
        currency="EUR",
        sale_price="15",
        sale_price_effective_date="2026-12-01T00:00:00Z/2026-12-31T23:59:59Z",
        product_highlights="Light\nWaterproof",
        attributes='{"fit":["slim"],"pattern":["solid","striped"]}',
    )
    await committed(worker, [rich], **v2)
    refused = await chunk(
        worker,
        [product("two", sale_price="1", price="2", currency="EUR", color=" ")],
        **v2,
    )
    assert refused.content == (
        b'{"error":"invalid_record","record":0,"field":"color","reason":"blank"}'
    )

    records = await human(worker, "GET", "records")
    assert records.status_code == 200, records.text
    by_kind = {row["kind"]: row for row in records.json()["records"]}
    assert by_kind["page"]["fields"] == page["fields"]
    assert by_kind["product"]["fields"]["attributes"] == rich["fields"]["attributes"]
    feeds_page = await human(worker, "GET", "activity/feeds")
    assert feeds_page.status_code == 200, feeds_page.text
    body = feeds_page.json()
    assert (body["schema_version"], body["schema_hash"]) == (2, SCHEMA_V2.schema_hash)
    assert {item["kind"] for item in body["items"]} == {"product", "page"}


# ----------------------------------------------------------------------------
# The switch and the upgrade operation
# ----------------------------------------------------------------------------

UPGRADE = {
    "target_schema_version": 2,
    "expected": {
        "schema_version": 1,
        "schema_hash": SCHEMA_V1.schema_hash,
        "generation": 1,
    },
}
UPGRADED = (
    b'{"schema_hash":"' + SCHEMA_V2.schema_hash.encode() + b'","schema_version":2,'
    b'"generation":1,"changed":'
)
NOT_FOUND = b'{"error":"not_found"}'
CONFLICT = b'{"error":"RES-002"}'
DISCOVERY_SWITCHES = (
    "oidc_enabled",
    "mcp_public_endpoints_enabled",
    "sync_integrations_enabled",
    "sync_credentials_enabled",
    "sync_runs_enabled",
)


@pytest_asyncio.fixture(loop_scope="session")
async def switched(feeds, monkeypatch):
    settings = get_settings()
    for flag in (*DISCOVERY_SWITCHES, "sync_schema_v2_enabled"):
        monkeypatch.setattr(settings, flag, True)
    return feeds


async def upgrade(worker, body=None, role="owner"):
    return await human(worker, "POST", "schema/upgrade", body=body or UPGRADE, role=role)


async def upgraded_events(worker) -> int:
    async with worker.factory() as db:
        return (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "sync.schema.upgraded",
                    AuditLog.user_metadata["installation_id"].as_string() == str(worker.iid),
                )
            )
        ) or 0


async def generation(worker) -> int:
    async with worker.factory() as db:
        return (await db.get(SyncInstallation, worker.iid)).generation


def schema_v2_bytes() -> bytes:
    return (FIXTURES / "sync-schema-v2.json").read_bytes().removesuffix(b"\n")


async def test_the_upgrade_answers_404_while_the_switch_is_off(feeds):
    response = await upgrade(feeds)
    assert (response.status_code, response.content) == (404, NOT_FOUND)
    assert await schema_rows(feeds) == [1] and await upgraded_events(feeds) == 0


async def test_an_upgrade_stores_version_two_once_and_keeps_the_generation(switched):
    worker = switched
    first = await upgrade(worker)
    assert first.status_code == 200, first.text
    assert first.content == UPGRADED + b"true}"
    assert first.headers["cache-control"] == "no-store"
    assert await schema_rows(worker) == [1, 2]
    assert await upgraded_events(worker) == 1 and await generation(worker) == 1
    # A retry, a second tab or a stale snapshot: already on version 2.
    for body in (
        UPGRADE,
        {**UPGRADE, "expected": {**UPGRADE["expected"], "generation": 99}},
        {
            **UPGRADE,
            "expected": {
                "schema_version": 2,
                "schema_hash": SCHEMA_V2.schema_hash,
                "generation": 1,
            },
        },
    ):
        again = await upgrade(worker, body)
        assert (again.status_code, again.content) == (200, UPGRADED + b"false}")
    assert await schema_rows(worker) == [1, 2] and await upgraded_events(worker) == 1

    schema = await service(worker, "GET", "schema")
    assert schema.content == schema_v2_bytes()
    v1_expected = {
        "config_version": 1,
        "generation": 1,
        "schema_hash": SCHEMA_V1.schema_hash,
        "schema_version": 1,
    }
    stale = await configure(worker, "product", worker.fid, expected=v1_expected)
    assert (stale.status_code, stale.content) == (409, CONFLICT)
    current = await configure(
        worker,
        "product",
        worker.fid,
        expected={
            **v1_expected,
            "schema_hash": SCHEMA_V2.schema_hash,
            "schema_version": 2,
        },
    )
    assert current.status_code == 200, current.text
    old = await service(worker, "POST", "runs", body=manifest(worker, [product("one")]))
    assert (old.status_code, old.content) == (400, b'{"error":"invalid_manifest"}')
    await committed(
        worker,
        [product("one", color="Blue")],
        schema_version=2,
        schema_hash=SCHEMA_V2.schema_hash,
    )


@pytest.mark.parametrize(
    "expected",
    [
        {"schema_version": 1, "schema_hash": SCHEMA_V1.schema_hash, "generation": 2},
        {"schema_version": 1, "schema_hash": "0" * 64, "generation": 1},
        {"schema_version": 2, "schema_hash": SCHEMA_V1.schema_hash, "generation": 1},
    ],
)
async def test_a_stale_snapshot_changes_nothing(switched, expected):
    response = await upgrade(switched, {**UPGRADE, "expected": expected})
    assert (response.status_code, response.content) == (409, CONFLICT)
    assert await schema_rows(switched) == [1] and await upgraded_events(switched) == 0


async def test_a_viewer_cannot_upgrade(switched):
    response = await upgrade(switched, role="viewer")
    assert response.status_code in (403, 404), response.text
    assert await schema_rows(switched) == [1] and await upgraded_events(switched) == 0


async def test_an_active_run_blocks_and_a_dead_run_does_not(switched):
    worker = switched
    run = await service(worker, "POST", "runs", body=manifest(worker, [product("one")]))
    assert run.status_code == 201, run.text
    blocked = await upgrade(worker)
    assert (blocked.status_code, blocked.content) == (
        409,
        b'{"error":"sync_schema_upgrade_runs_active"}',
    )
    assert await schema_rows(worker) == [1] and await upgraded_events(worker) == 0
    # Past its window the run no longer counts.
    now = worker.store.clock.now
    async with worker.factory() as db:
        await db.execute(
            update(SyncRun)
            .where(SyncRun.id == UUID(run.json()["run_id"]))
            .values(created_at=now - timedelta(days=2), expires_at=now - timedelta(days=1))
        )
        await db.commit()
    assert (await upgrade(worker)).content == UPGRADED + b"true}"


async def test_an_aborted_run_does_not_block(switched):
    worker = switched
    run = await service(worker, "POST", "runs", body=manifest(worker, [product("one")]))
    assert (await upgrade(worker)).status_code == 409
    aborted = await service(worker, "POST", f"runs/{run.json()['run_id']}/abort")
    assert aborted.status_code == 200, aborted.text
    assert (await upgrade(worker)).content == UPGRADED + b"true}"


async def test_no_schema_or_an_unregistered_schema_is_refused(switched):
    worker = switched
    async with worker.factory() as db:
        installation = await db.get(SyncInstallation, worker.iid)
        row = await db.scalar(
            select(ResourceSchema).where(ResourceSchema.resource_pk == installation.resource_pk)
        )
        row.field_definitions = [*row.field_definitions, {"name": "extra"}]
        await db.commit()
    unregistered = await upgrade(worker)
    assert (unregistered.status_code, unregistered.content) == (409, CONFLICT)
    async with worker.factory() as db:
        installation = await db.get(SyncInstallation, worker.iid)
        await db.execute(delete(SyncFeed).where(SyncFeed.installation_id == worker.iid))
        await db.execute(
            delete(ResourceSchema).where(ResourceSchema.resource_pk == installation.resource_pk)
        )
        await db.commit()
    missing = await upgrade(worker)
    assert (missing.status_code, missing.content) == (409, CONFLICT)
    assert await schema_rows(worker) == [] and await upgraded_events(worker) == 0


async def test_the_upgrade_clears_resume_candidates_and_the_preview(switched):
    worker = switched
    run = await service(worker, "POST", "runs", body=manifest(worker, [product("one")]))
    run_id = UUID(run.json()["run_id"])
    assert (await service(worker, "POST", f"runs/{run_id}/abort")).status_code == 200
    now = worker.store.clock.now
    async with worker.factory() as db:
        db.add(
            SyncResumeCandidate(
                id=run_id,
                installation_id=worker.iid,
                feed_id=worker.fid,
                active=True,
                epoch=1,
                generations=[1, 1, 1],
                manifest={},
                records=[product("one")],
                record_count=1,
                byte_count=10,
                snapshot_hash="3" * 64,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        db.add(
            SyncResumePreview(
                installation_id=worker.iid,
                ownership_epoch=1,
                epoch=1,
                generation=1,
                manual_hash="4" * 64,
                source_hash="5" * 64,
                diff_hash="6" * 64,
                totals={},
                diff=[],
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            )
        )
        await db.commit()
    assert (await upgrade(worker)).content == UPGRADED + b"true}"
    async with worker.factory() as db:
        candidate = await db.get(SyncResumeCandidate, run_id)
        assert (candidate.active, candidate.records) == (False, None)
        assert await db.get(SyncResumePreview, worker.iid) is None


async def test_a_records_cursor_and_a_manual_request_binding_end(switched):
    from services.sync_requests import source_version

    worker = switched
    await committed(worker, [product("one"), product("two")])
    page = await human(worker, "GET", "records", params={"limit": 1})
    assert page.status_code == 200, page.text
    cursor = page.json()["next_cursor"]
    assert cursor
    async with worker.factory() as db:
        before = await source_version(db, await db.get(SyncInstallation, worker.iid))
        await db.rollback()
    assert (await upgrade(worker)).content == UPGRADED + b"true}"
    stale = await human(worker, "GET", "records", params={"limit": 1, "cursor": cursor})
    assert (stale.status_code, stale.content) == (409, CONFLICT)
    async with worker.factory() as db:
        after = await source_version(db, await db.get(SyncInstallation, worker.iid))
        await db.rollback()
    assert after != before
    fresh = await human(worker, "GET", "records")
    assert fresh.status_code == 200 and len(fresh.json()["records"]) == 2


async def test_a_new_installation_gets_version_two_and_keeps_it_when_the_switch_goes_off(
    switched, monkeypatch
):
    worker = switched
    async with worker.factory() as db:
        installation = await db.get(SyncInstallation, worker.iid)
        await db.execute(delete(SyncFeed).where(SyncFeed.installation_id == worker.iid))
        await db.execute(
            delete(ResourceSchema).where(ResourceSchema.resource_pk == installation.resource_pk)
        )
        await db.commit()
    first = await configure(worker, "page", worker.fid)
    assert first.status_code == 200, first.text
    assert (first.json()["schema_version"], first.json()["schema_hash"]) == (
        2,
        SCHEMA_V2.schema_hash,
    )
    assert await schema_rows(worker) == [2]
    monkeypatch.setattr(get_settings(), "sync_schema_v2_enabled", False)
    review = await configure(worker, "review")
    assert review.status_code == 200, review.text
    assert (await service(worker, "GET", "schema")).content == schema_v2_bytes()
    off = await upgrade(worker)
    assert (off.status_code, off.content) == (404, NOT_FOUND)
    assert await upgraded_events(worker) == 0
