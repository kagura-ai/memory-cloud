"""Real HTTP and PostgreSQL sessions for staged feed snapshots."""

from uuid import UUID, uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from api.routes import sync_feeds, sync_records
from models.auth import ContextMember, WorkspaceMember
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from models.sync_feed_schemas import SCHEMA_HASH, snapshot_digest
from services import (
    sync_feed_authority,
    sync_feed_commit,
    sync_feed_human,
    sync_feed_index,
    sync_feed_runs,
)
from tests.integration import sync_credential_fixtures as credentials

store = credentials.store
worker = credentials.worker


@pytest_asyncio.fixture(loop_scope="session")
async def feeds(worker, monkeypatch):
    monkeypatch.setattr(sync_feed_authority.get_settings(), "sync_runs_enabled", True)
    for module in (
        sync_feed_authority,
        sync_feed_commit,
        sync_feed_human,
        sync_feed_index,
        sync_feed_runs,
    ):
        monkeypatch.setattr(module, "utcnow", lambda: worker.store.clock.now)
    worker.store.app.include_router(sync_feeds.router, prefix="/api/v1")
    worker.store.app.include_router(sync_records.router, prefix="/api/v1")
    async with worker.factory() as db:
        client = await db.get(OIDCClient, worker.store.client_id)
        client.scope += " sync.records:read"
        for model, ids in (
            (OIDCToken, worker.store.token_ids.values()),
            (OIDCGrant, worker.store.grant_ids.values()),
        ):
            for row in (await db.scalars(select(model).where(model.id.in_(list(ids))))).all():
                row.scope += " sync.records:read"
        db.add(
            ContextMember(
                context_id=UUID(worker.target["context_id"]),
                user_id=worker.store.users["member"],
                role="editor",
            )
        )
        member = await db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == worker.store.wid,
                WorkspaceMember.user_id == worker.store.users["member"],
            )
        )
        member.allowed_context_ids = [UUID(worker.target["context_id"])]
        await db.commit()
    worker.iid = UUID(worker.target["installation_id"])
    worker.fid = uuid4()
    worker.token = (await credentials.mint(worker))["access_token"]
    result = await human(
        worker, "PUT", f"feeds/{worker.fid}", body={"kind": "product", "config_hash": "1" * 64}
    )
    assert result.status_code == 200, result.text
    return worker


async def request(worker, method, path, *, token, body=None, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=worker.store.app), base_url="https://test"
    ) as client:
        return await client.request(
            method, path, json=body, headers={"Authorization": "Bearer " + token}, **kwargs
        )


async def service(worker, method, suffix, *, body=None, token=None, **kwargs):
    return await request(
        worker,
        method,
        f"/api/v1/sync/installations/{worker.iid}/{suffix}",
        token=token or worker.token,
        body=body,
        **kwargs,
    )


async def human(
    worker, method, suffix, *, body=None, role="owner", workspace=None, installation=None, **kwargs
):
    return await request(
        worker,
        method,
        f"/api/v1/workspaces/{workspace or worker.store.wid}/sync-installations/{installation or worker.iid}/{suffix}",
        token=worker.store.tokens[role],
        body=body,
        **kwargs,
    )


def product(eid="one", **fields):
    return {
        "external_id": eid,
        "fields": {
            "title": "Public product " + eid,
            "url": "https://brand.example.test/item/" + eid,
            **fields,
        },
    }


def manifest(worker, records, *, chunks=None, **extra):
    return {
        "feed_id": str(worker.fid),
        "operation_id": str(uuid4()),
        "config_hash": "1" * 64,
        "source_hash": "2" * 64,
        "config_version": 1,
        "schema_version": 1,
        "schema_hash": SCHEMA_HASH,
        "snapshot_hash": snapshot_digest(records),
        "chunks": (1 if records else 0) if chunks is None else chunks,
        "records": len(records),
        **extra,
    }


async def staged(worker, records, **extra):
    result = await service(
        worker,
        "POST",
        "runs",
        body=manifest(worker, records, chunks=(len(records) + 99) // 100, **extra),
    )
    assert result.status_code == 201, result.text
    row = result.json()
    for start in range(0, len(records), 100):
        result = await service(
            worker,
            "PUT",
            f"runs/{row['run_id']}/chunks/{start // 100}",
            body={"records": records[start : start + 100]},
        )
        assert result.status_code == 200, result.text
    result = await service(worker, "POST", f"runs/{row['run_id']}/prepare")
    assert result.status_code == 200, result.text
    return result.json()


async def committed(worker, records, **extra):
    row = await staged(worker, records, **extra)
    result = await service(
        worker, "POST", f"runs/{row['run_id']}/commit", body={"diff_hash": row["diff_hash"]}
    )
    assert result.status_code == 200, result.text
    return result.json()
