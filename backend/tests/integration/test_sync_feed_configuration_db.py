"""Real current authority and atomic feed configuration snapshots, never worker keys in Web."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from models.auth import AuditLog, Context, WorkspaceMember
from models.management_schemas import digest
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from models.public_usage_budget import PublicUsageBudget
from models.resource import ResourceSchema
from models.sync_feed import SyncFeed
from models.sync_feed_schemas import SCHEMA_HASH
from models.sync_installation import SyncInstallation, SyncService
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_feed_fixtures import committed, human, product
from tests.integration.sync_feed_fixtures import request as authenticated_request

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


@pytest_asyncio.fixture(loop_scope="session")
async def configured(feeds, monkeypatch):
    from api.routes import sync_activity as routes
    from services import sync_activity

    monkeypatch.setattr(sync_activity.get_settings(), "sync_observability_enabled", True)
    monkeypatch.setattr(sync_activity, "utcnow", lambda: feeds.store.clock.now)
    feeds.store.app.include_router(routes.router, prefix="/api/v1")
    return feeds


def expected(**changes):
    return {
        "config_version": 1,
        "generation": 1,
        "schema_hash": SCHEMA_HASH,
        "schema_version": 1,
        **changes,
    }


async def put(configured, *, value="a", snapshot=None, fid=None, role="owner"):
    body = {"kind": "product", "config_hash": value * 64}
    if snapshot is not None:
        body["expected"] = snapshot
    return await human(configured, "PUT", f"feeds/{fid or configured.fid}", body=body, role=role)


async def saved(configured):
    async with configured.factory() as db:
        row = await db.get(SyncFeed, configured.fid)
        audits = await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "sync.feed.configured",
                AuditLog.user_metadata["installation_id"].as_string() == str(configured.iid),
            )
        )
        return row.config_hash, row.config_version, audits


async def test_current_feed_metadata_is_normal_viewer_body_free_and_passive(configured):
    await committed(configured, [product(description="private-body-must-not-appear")])
    async with configured.factory() as db:
        for model, key in (
            (OIDCToken, configured.store.token_ids["viewer"]),
            (OIDCGrant, configured.store.grant_ids["viewer"]),
            (OIDCClient, configured.store.client_id),
        ):
            row = await db.get(model, key)
            row.scope = "openid workspaces:read"
        await db.commit()
        before = await db.scalar(select(func.count()).select_from(PublicUsageBudget))
    page = await human(configured, "GET", "activity/feeds", role="viewer")
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"] == "no-store"
    body = page.json()
    assert set(body) == {
        "workspace_id",
        "installation_id",
        "generation",
        "schema_hash",
        "schema_version",
        "as_of",
        "items",
    }
    assert body["generation"] == 1 and body["schema_hash"] == SCHEMA_HASH
    assert body["schema_version"] == 1 and len(body["items"]) == 1
    assert set(body["items"][0]) == {"feed_id", "kind", "config_hash", "config_version", "revision"}
    detail = await human(configured, "GET", f"activity/feeds/{configured.fid}", role="viewer")
    assert detail.status_code == 200, detail.text
    assert detail.json()["config_version"] == 1 and detail.json()["revision"] == 1
    for response in (page, detail):
        for private in (
            "private-body-must-not-appear",
            "source_url",
            "fields",
            "diff",
            "access_token",
        ):
            assert private not in response.text
    assert (await human(configured, "GET", "records", role="viewer")).status_code == 403
    async with configured.factory() as db:
        assert await db.scalar(select(func.count()).select_from(PublicUsageBudget)) == before


async def test_delayed_original_update_cannot_replace_a_newer_configuration(configured):
    release, entered = asyncio.Event(), asyncio.Event()

    async def delayed_original():
        entered.set()
        await release.wait()
        return await put(configured, value="a", snapshot=expected())

    pending = asyncio.create_task(delayed_original())
    try:
        await entered.wait()
        newer = await put(configured, value="b", snapshot=expected())
        assert newer.status_code == 200, newer.text
        before = await saved(configured)
        release.set()
        old = await pending
        assert old.status_code == 409, old.text
        assert await saved(configured) == before
        assert before[:2] == ("b" * 64, 2)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_two_real_sessions_cannot_both_apply_the_same_expected_version(configured):
    results = await asyncio.gather(
        put(configured, value="a", snapshot=expected()),
        put(configured, value="b", snapshot=expected()),
    )
    assert sorted(response.status_code for response in results) == [200, 409]
    before = await saved(configured)
    assert before[1] == 2
    assert (await put(configured, value="c", snapshot=expected())).status_code == 409
    assert await saved(configured) == before


async def test_aba_and_same_hash_cannot_disguise_a_stale_precondition(configured):
    assert (await put(configured, value="b", snapshot=expected())).status_code == 200
    assert (await put(configured, value="1")).status_code == 200  # legacy remains unguarded
    before = await saved(configured)
    assert before[:2] == ("1" * 64, 3)
    assert (await put(configured, value="1", snapshot=expected())).status_code == 409
    assert await saved(configured) == before
    same = await put(configured, value="1", snapshot=expected(config_version=3))
    assert same.status_code == 200 and same.json()["config_version"] == 3


async def test_guarded_update_of_an_absent_feed_requires_create_only_version(configured):
    """An absent feed is observed as version 0; a nonzero expectation is stale, not a create."""
    fid = uuid4()
    for version in (1, 2):
        response = await put(configured, fid=fid, snapshot=expected(config_version=version))
        assert response.status_code == 409, response.text
    async with configured.factory() as db:
        assert await db.get(SyncFeed, fid) is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "sync.feed.configured",
                    AuditLog.user_metadata["feed_id"].as_string() == str(fid),
                )
            )
            == 0
        )


async def test_feed_of_another_installation_is_not_found_and_never_changes(configured):
    """A feed UUID from another workspace's installation is never read or reconfigured here."""
    store = configured.store
    foreign_iid, foreign_fid = uuid4(), uuid4()
    async with configured.factory() as db:
        db.add(
            SyncInstallation(
                id=foreign_iid,
                workspace_id=store.other,
                service_id=store.service_id,
                service_client_id=store.service_id + "-worker",
                authorization_id=uuid4(),
                application_id="foreign-application",
                operations=[],
                resource_pk=uuid4(),
                context_id=uuid4(),
                endpoint_id=uuid4(),
                resource_id="foreign-resource",
            )
        )
        await db.flush()
        db.add(
            SyncFeed(
                id=foreign_fid,
                installation_id=foreign_iid,
                kind="product",
                config_hash="9" * 64,
            )
        )
        await db.commit()
    detail = await human(configured, "GET", f"activity/feeds/{foreign_fid}")
    assert detail.status_code == 404, detail.text
    assert "9" * 64 not in detail.text and str(foreign_iid) not in detail.text
    page = await human(configured, "GET", "activity/feeds")
    assert page.status_code == 200, page.text
    assert [item["feed_id"] for item in page.json()["items"]] == [str(configured.fid)]
    before = await saved(configured)
    for snapshot in (None, expected(), expected(config_version=0)):
        response = await put(configured, fid=foreign_fid, value="a", snapshot=snapshot)
        assert response.status_code == 404, response.text
    async with configured.factory() as db:
        foreign = await db.get(SyncFeed, foreign_fid)
        assert (foreign.installation_id, foreign.config_hash, foreign.config_version) == (
            foreign_iid,
            "9" * 64,
            1,
        )
    assert await saved(configured) == before


async def test_create_only_does_not_become_a_second_update(configured):
    assert (await put(configured, snapshot=expected(config_version=0))).status_code == 409
    fid = uuid4()
    first = await put(configured, fid=fid, snapshot=expected(config_version=0))
    assert first.status_code == 200 and first.json()["config_version"] == 1
    duplicate = await put(configured, fid=fid, snapshot=expected(config_version=0))
    assert duplicate.status_code == 409


@pytest.mark.parametrize(
    "change",
    [
        {"config_version": 7},
        {"generation": 2},
        {"schema_version": 2},
        {"schema_hash": "0" * 64},
        {"schema_hash": None, "schema_version": None},
    ],
)
async def test_any_changed_original_snapshot_rejects_before_config_or_success_audit(
    configured, change
):
    before = await saved(configured)
    response = await put(configured, snapshot=expected(**change))
    assert response.status_code == 409, response.text
    assert await saved(configured) == before


async def test_absent_schema_is_observed_and_a_stale_create_cannot_insert_one(configured):
    resource = UUID(configured.target["resource_pk"])
    async with configured.factory() as db:
        await db.execute(delete(ResourceSchema).where(ResourceSchema.resource_pk == resource))
        await db.commit()
    page = await human(configured, "GET", "activity/feeds")
    assert page.status_code == 200, page.text
    assert page.json()["schema_hash"] is None and page.json()["schema_version"] is None
    fid = uuid4()
    response = await put(
        configured,
        fid=fid,
        snapshot=expected(config_version=0, generation=2, schema_hash=None, schema_version=None),
    )
    assert response.status_code == 409
    async with configured.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ResourceSchema)
                .where(ResourceSchema.resource_pk == resource)
            )
            == 0
        )
        assert await db.get(SyncFeed, fid) is None
    good = await put(
        configured,
        fid=fid,
        snapshot=expected(config_version=0, schema_hash=None, schema_version=None),
    )
    assert good.status_code == 200 and good.json()["schema_version"] == 1


async def test_matching_precondition_never_extends_original_authority_at_commit(
    configured, monkeypatch
):
    from services import sync_audit

    before, original_time = await saved(configured), configured.store.clock.now
    original_audit = sync_audit.add_audit

    def expire_after_audit(*args, **kwargs):
        value = original_audit(*args, **kwargs)
        configured.store.clock.now += timedelta(hours=9)
        return value

    monkeypatch.setattr(sync_audit, "add_audit", expire_after_audit)
    try:
        response = await put(configured, snapshot=expected())
        assert response.status_code == 409, response.text
    finally:
        configured.store.clock.now = original_time
    assert await saved(configured) == before


async def test_metadata_is_exact_target_client_scope_and_feature_bound(configured, monkeypatch):
    from services import sync_activity

    for suffix in ("activity/feeds", f"activity/feeds/{configured.fid}"):
        assert (await human(configured, "GET", suffix, workspace=uuid4())).status_code == 404
        assert (await human(configured, "GET", suffix, installation=uuid4())).status_code == 404
        assert (await human(configured, "GET", suffix, role="outsider")).status_code == 404
        service = await authenticated_request(
            configured,
            "GET",
            f"/api/v1/workspaces/{configured.store.wid}/sync-installations/{configured.iid}/{suffix}",
            token=configured.token,
        )
        assert service.status_code == 401
        for query in ("?source_url=private", "?limit=20", "?limit=1&limit=2"):
            assert (await human(configured, "GET", suffix + query)).status_code == 400
    assert (await human(configured, "GET", f"activity/feeds/{uuid4()}")).status_code == 404
    async with configured.factory() as db:
        grant = await db.get(OIDCGrant, configured.store.grant_ids["owner"])
        grant.client_id = configured.store.foreign_client
        await db.commit()
    assert (await human(configured, "GET", "activity/feeds")).status_code == 404
    monkeypatch.setattr(sync_activity.get_settings(), "sync_observability_enabled", False)
    assert (await human(configured, "GET", "activity/feeds")).status_code == 404


async def test_current_editor_and_version_counter_bounds_still_apply(configured):
    assert (await put(configured, snapshot=expected(), role="viewer")).status_code == 404
    async with configured.factory() as db:
        row = await db.get(SyncFeed, configured.fid)
        row.config_version = 2**31 - 1
        await db.commit()
    before = await saved(configured)
    assert (await put(configured, snapshot=expected(config_version=2**31 - 1))).status_code == 409
    assert await saved(configured) == before
    same = await put(configured, value="1", snapshot=expected(config_version=2**31 - 1))
    assert same.status_code == 200 and same.json()["config_version"] == 2**31 - 1
    invalid = await put(configured, snapshot=expected(config_version=2**31))
    assert invalid.status_code == 400


@pytest.mark.parametrize("endpoint", ["list", "detail", "configure"])
async def test_schema_row_contention_is_private_conflict_and_rolls_back(configured, endpoint):
    before = await saved(configured)
    async with configured.factory() as blocker:
        schema = await blocker.scalar(
            select(ResourceSchema)
            .where(ResourceSchema.resource_pk == UUID(configured.target["resource_pk"]))
            .with_for_update()
        )
        assert schema is not None
        if endpoint == "configure":
            response = await put(configured, snapshot=expected())
        else:
            suffix = "activity/feeds" + (f"/{configured.fid}" if endpoint == "detail" else "")
            response = await human(configured, "GET", suffix)
        assert response.status_code == 409, response.text
        assert response.headers["cache-control"] == "no-store"
        assert "resource_schemas" not in response.text and "55P03" not in response.text
        await blocker.rollback()
    assert await saved(configured) == before
    # The rejected request must not leave authority locks or a poisoned session behind.
    assert (await put(configured, snapshot=expected())).status_code == 200


async def test_observation_reports_actual_schema_without_overriding_fixed_schema_policy(configured):
    fields = [{"name": "private-example", "type": "string", "classification": "private"}]
    async with configured.factory() as db:
        schema = await db.scalar(
            select(ResourceSchema).where(
                ResourceSchema.resource_pk == UUID(configured.target["resource_pk"])
            )
        )
        schema.field_definitions = fields
        await db.commit()
    response = await human(configured, "GET", f"activity/feeds/{configured.fid}")
    assert response.status_code == 200, response.text
    assert response.json()["schema_hash"] == digest(fields) != SCHEMA_HASH
    assert "private-example" not in response.text and "classification" not in response.text
    before = await saved(configured)
    result = await put(configured, snapshot=expected(schema_hash=digest(fields)))
    assert result.status_code == 409
    assert await saved(configured) == before


@pytest.mark.parametrize("change", ["revoked", "service_disabled"])
async def test_observation_preserves_post_revocation_diagnostics_but_never_write_authority(
    configured, change
):
    async with configured.factory() as db:
        installation = await db.get(SyncInstallation, configured.iid)
        if change == "revoked":
            installation.state = "revoked"
            installation.revoked_at = configured.store.clock.now
        else:
            (await db.get(SyncService, installation.service_id)).enabled = False
        await db.commit()
    for suffix in ("activity/feeds", f"activity/feeds/{configured.fid}"):
        response = await human(configured, "GET", suffix, role="viewer")
        assert response.status_code == 200, response.text
    assert (await put(configured, snapshot=expected())).status_code == 404


@pytest.mark.parametrize("change", ["membership", "reach", "private"])
async def test_observation_requires_current_membership_and_context_reach(configured, change):
    async with configured.factory() as db:
        member_query = select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == configured.store.wid,
            WorkspaceMember.user_id == configured.store.users["member"],
        )
        if change == "membership":
            await db.delete(await db.scalar(member_query))
        elif change == "reach":
            (await db.scalar(member_query)).allowed_context_ids = []
        else:
            (await db.get(Context, UUID(configured.target["context_id"]))).is_private = True
        await db.commit()
    for suffix in ("activity/feeds", f"activity/feeds/{configured.fid}"):
        response = await human(configured, "GET", suffix, role="member")
        assert response.status_code == 404, response.text
        assert "config_hash" not in response.text


@pytest.mark.parametrize(
    "invalid",
    [None, {"config_version": 1}, expected(generation=1.0), expected(unknown=True)],
)
async def test_invalid_expected_http_body_cannot_mutate_or_audit(configured, invalid):
    before = await saved(configured)
    response = await human(
        configured,
        "PUT",
        f"feeds/{configured.fid}",
        body={"kind": "product", "config_hash": "a" * 64, "expected": invalid},
    )
    assert response.status_code == 400
    assert await saved(configured) == before
