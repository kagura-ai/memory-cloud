"""Human data scope, live visibility, canonical fields and revision-bound pagination."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from models.auth import Context, WorkspaceMember
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from models.resource import ResourceSchema
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_feed_fixtures import committed, human, product

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


async def test_human_reads_canonical_public_fields_without_a_service_key(feeds):
    await committed(feeds, [product("one"), product("two")])
    first = await human(feeds, "GET", "records?limit=1", role="viewer")
    assert first.status_code == 200, first.text
    assert len(first.json()["records"]) == 1 and first.json()["next_cursor"]
    assert set(first.json()["records"][0]) == {
        "feed_id",
        "kind",
        "external_id",
        "version",
        "fields",
    }
    assert first.json()["records"][0]["fields"]["url"].startswith("https://")
    second = await human(
        feeds, "GET", "records?limit=1&cursor=" + first.json()["next_cursor"], role="viewer"
    )
    assert second.status_code == 200 and second.json()["next_cursor"] is None
    assert second.json()["records"][0]["external_id"] != first.json()["records"][0]["external_id"]


@pytest.mark.parametrize("authority", [OIDCToken, OIDCGrant, OIDCClient])
async def test_workspace_read_scope_never_implies_data_read(feeds, authority):
    await committed(feeds, [product()])
    async with feeds.factory() as db:
        key = (
            feeds.store.token_ids["owner"]
            if authority is OIDCToken
            else feeds.store.grant_ids["owner"]
            if authority is OIDCGrant
            else feeds.store.client_id
        )
        row = await db.get(authority, key)
        row.scope = row.scope.replace(" sync.records:read", "")
        await db.commit()
    result = await human(feeds, "GET", "records")
    assert result.status_code == 403 and result.json()["error"] == "insufficient_scope"
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "change", ["membership", "reach", "private", "schema", "outsider", "workspace", "installation"]
)
async def test_current_denials_never_return_record_bodies(feeds, change):
    await committed(feeds, [product()])
    role, extra = "member", {}
    async with feeds.factory() as db:
        if change == "membership":
            await db.execute(
                delete(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == feeds.store.wid,
                    WorkspaceMember.user_id == feeds.store.users[role],
                )
            )
        elif change == "reach":
            row = await db.scalar(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == feeds.store.wid,
                    WorkspaceMember.user_id == feeds.store.users[role],
                )
            )
            row.allowed_context_ids = []
        elif change == "private":
            (await db.get(Context, UUID(feeds.target["context_id"]))).is_private = True
        elif change == "schema":
            row = await db.scalar(
                select(ResourceSchema).where(
                    ResourceSchema.resource_pk == UUID(feeds.target["resource_pk"])
                )
            )
            row.field_definitions = [
                {**field, "classification": "private"} for field in row.field_definitions
            ]
        elif change == "outsider":
            role = "outsider"
        elif change == "workspace":
            extra["workspace"] = feeds.store.other
        else:
            extra["installation"] = uuid4()
        await db.commit()
    result = await human(feeds, "GET", "records", role=role, **extra)
    assert result.status_code in {404, 409}, result.text
    assert "Public product" not in result.text


async def test_cursor_cannot_cross_dataset_revision_or_feed_filter(feeds):
    await committed(feeds, [product("one"), product("two")])
    first = (await human(feeds, "GET", "records?limit=1")).json()
    assert first["next_cursor"]
    wrong_filter = await human(
        feeds, "GET", f"records?limit=1&feed_id={feeds.fid}&cursor=" + first["next_cursor"]
    )
    assert wrong_filter.status_code == 409
    await committed(feeds, [product("one", title="updated"), product("two")])
    assert (await human(feeds, "GET", "records?cursor=" + first["next_cursor"])).status_code == 409


async def test_faq_feed_shares_resource_but_cannot_delete_product_feed(feeds):
    await committed(feeds, [product()])
    faq = uuid4()
    result = await human(
        feeds, "PUT", f"feeds/{faq}", body={"kind": "faq", "config_hash": "1" * 64}, role="member"
    )
    assert result.status_code == 200, result.text
    feeds.fid = faq
    await committed(
        feeds,
        [
            {
                "external_id": "question-1",
                "fields": {"question": "What is this?", "answer": "Approved answer"},
            }
        ],
    )
    result = await human(feeds, "GET", "records")
    assert result.status_code == 200
    assert {record["kind"] for record in result.json()["records"]} == {"product", "faq"}
