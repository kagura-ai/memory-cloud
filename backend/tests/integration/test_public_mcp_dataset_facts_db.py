"""The public endpoint's dataset counts on PostgreSQL (#2995).

``count_dataset_facts`` counts the documents the endpoint serves, per sync
feed kind: a product feed's records and an FAQ feed's records give two groups,
rows with no sync record are "other". A deleted row, a member's hand-written
note in the same context (no ``resource_id``), another context's rows and
older versions of a document are not counted. The statement runs under the
two-second statement timeout.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from mcp_server.public.facts import DatasetFacts, count_dataset_facts
from mcp_server.public.publication import ServingEndpoint
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import Memory
from models.resource import Resource, ResourceEvent
from models.sync_feed import SyncFeed, SyncRecord
from models.sync_installation import SyncInstallation, SyncService
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _memory(ws: UUID, ctx: UUID, author: str, **kw: Any) -> Memory:
    slug, doc, version = kw.pop("slug", None), kw.pop("doc", None), kw.pop("version", 1)
    details = (
        {"resource_id": slug, "doc_id": doc, "version": version}
        if slug
        else {"note": "hand-written"}
    )
    return Memory(
        id=uuid4(),
        workspace_id=ws,
        context_id=ctx,
        user_id=author,
        summary=kw.pop("summary", "entry"),
        content="entry text",
        type="resource_data",
        scope="persistent",
        client="test",
        importance=0.5,
        embedding_status="success",
        tags=[],
        details=details,
        **kw,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[SimpleNamespace]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner = f"dfo-{tag}"
    slug = f"shop-{tag}"
    async with factory() as s:
        s.add(User(email=f"{owner}@example.test", user_id=owner, name=owner, role="user"))
        await s.flush()
        ws = Workspace(
            id=uuid4(),
            name=f"Example Shop {tag}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=50000,
            weekly_api_limit=250000,
        )
        s.add(ws)
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
        resource = Resource(workspace_id=ws.id, resource_id=slug, created_by=owner)
        ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=slug,
            created_by=owner,
            is_private=False,
            is_public=True,
            resource_id=slug,
        )
        other_ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"notes-{tag}",
            created_by=owner,
            is_private=True,
            is_public=False,
        )
        s.add_all([resource, ctx, other_ctx])
        await s.flush()
        service = SyncService(
            service_id=f"svc-{tag}",
            browser_client_id=f"browser-{tag}",
            service_client_id=f"service-{tag}",
        )
        s.add(service)
        await s.flush()
        installation = SyncInstallation(
            workspace_id=ws.id,
            service_id=service.service_id,
            service_client_id=service.service_client_id,
            authorization_id=uuid4(),
            application_id=f"app-{tag}",
            operations=["records:write"],
            resource_pk=resource.id,
            context_id=ctx.id,
            endpoint_id=uuid4(),
            resource_id=slug,
        )
        s.add(installation)
        await s.flush()
        feeds = {
            kind: SyncFeed(
                id=uuid4(),
                installation_id=installation.id,
                kind=kind,
                config_hash="1" * 64,
                config_version=1,
                revision=1,
            )
            for kind in ("product", "faq", "page", "review")
        }
        s.add_all(list(feeds.values()))
        event = ResourceEvent(
            resource_pk=resource.id,
            resource_id=slug,
            op="upsert",
            doc_id="seed",
            version=1,
            payload={},
            created_at=utcnow(),
        )
        s.add(event)
        await s.flush()
        records = [
            ("product", "sync:p1"),
            ("product", "sync:p2"),
            ("product", "sync:p3"),
            ("faq", "sync:f1"),
            ("faq", "sync:f2"),
            # #3055: the kinds of record schema version 2.
            ("page", "sync:g1"),
            ("page", "sync:g2"),
            ("page", "sync:g3"),
            ("review", "sync:r1"),
        ]
        for kind, doc in records:
            s.add(
                SyncRecord(
                    feed_id=feeds[kind].id,
                    resource_pk=resource.id,
                    external_id=doc,
                    doc_id=doc,
                    version=1,
                    payload={"title": doc},
                    last_event_id=event.id,
                )
            )
        rows = [
            # p1 in two versions counts once; p3 is deleted and not counted.
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:p1", version=1),
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:p1", version=2),
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:p2"),
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:p3", deleted_at=utcnow()),
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:f1"),
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:f2"),
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:g1"),
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:g2"),
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:g3"),
            _memory(ws.id, ctx.id, owner, slug=slug, doc="sync:r1"),
            # Ingested through the plain resource API: no sync record.
            _memory(ws.id, ctx.id, owner, slug=slug, doc="manual-1"),
            # A member's hand-written note in the same context.
            _memory(ws.id, ctx.id, owner),
            # Another context of the workspace, same slug in details.
            _memory(ws.id, other_ctx.id, owner, slug=slug, doc="sync:p2"),
        ]
        s.add_all(rows)
        await s.commit()
        endpoint = ServingEndpoint(
            id=installation.endpoint_id,
            public_id="pmcp_" + uuid4().hex[:22],
            workspace_id=ws.id,
            context_id=ctx.id,
            resource_pk=resource.id,
            resource_id=slug,
            public_name="Example Shop",
            public_description=None,
            public_usage_guide=None,
            context_name=slug,
            owner_user_id=owner,
        )
    try:
        yield SimpleNamespace(
            factory=factory, endpoint=endpoint, ws=ws.id, ctx=ctx.id, slug=slug, owner=owner
        )
    finally:
        async with factory() as s:
            await s.execute(delete(Memory).where(Memory.workspace_id == ws.id))
            await s.execute(delete(SyncRecord).where(SyncRecord.resource_pk == resource.id))
            await s.execute(delete(SyncInstallation).where(SyncInstallation.workspace_id == ws.id))
            await s.execute(delete(SyncService).where(SyncService.service_id == service.service_id))
            await s.execute(delete(ResourceEvent).where(ResourceEvent.resource_pk == resource.id))
            await s.execute(delete(Context).where(Context.workspace_id == ws.id))
            await s.execute(delete(Resource).where(Resource.workspace_id == ws.id))
            await s.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == ws.id))
            await s.execute(delete(Workspace).where(Workspace.id == ws.id))
            await s.execute(delete(User).where(User.user_id == owner))
            await s.commit()


async def test_counts_are_live_documents_per_kind(world) -> None:
    async with world.factory() as db:
        assert await count_dataset_facts(db, world.endpoint) == DatasetFacts(
            products=2, faq=2, pages=3, reviews=1, other=1
        )


async def test_the_statement_runs_under_the_two_second_timeout(world) -> None:
    async with world.factory() as db:
        await count_dataset_facts(db, world.endpoint)
        assert (await db.execute(text("SHOW statement_timeout"))).scalar() == "2s"
        await db.rollback()
        assert (await db.execute(text("SHOW statement_timeout"))).scalar() != "2s"


async def test_an_empty_dataset_counts_zero(world) -> None:
    other = dataclasses.replace(world.endpoint, resource_id=f"empty-{world.slug}")
    async with world.factory() as db:
        assert await count_dataset_facts(db, other) == DatasetFacts()
