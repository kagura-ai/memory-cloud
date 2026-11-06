"""documents/resolve on real rows (#2679, AC 6).

Opaque doc keys resolve only through a publication that serves right now and
only to what anonymous ``recall`` already shows: the title (summary cut to
200) and an http(s) source URL. Unpublish, switch off, a deleted memory,
another resource's key and a reused slug all answer ``null``; a capacity lock
or plan pause answers 503.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update

from config.settings import get_settings
from models.auth import Context
from models.memory import Memory
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_usage import PublicMcpDocKey
from models.resource import Resource
from services.public_mcp_usage_events import doc_key
from tests.observability_helpers import World, app_client, bearer, issue_credential, world_rows
from utils.datetime import utcnow
from utils.exceptions import CapacityLockedError, QuotaExceededError

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "/api/v1/observability/documents/resolve"
DOC_MARKER = "zq-doc-marker-6610"
LONG_SUMMARY = "Blue widget " + "with a very long description " * 20


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine):
    async with world_rows(async_engine) as w:
        yield w


@pytest.fixture(autouse=True)
def switches(monkeypatch):
    monkeypatch.setattr(get_settings(), "observability_api_enabled", True)
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)


def _key(w: World, resource: str, doc_id: str) -> str:
    return doc_key(w.resource_pk[resource], doc_id, get_settings().audit_hmac_key)


def _memory(w: World, key: str, doc_id: str, summary: str, **kw: Any) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=w.ws,
        context_id=kw.pop("context_id", w.ctx[key]),
        user_id=w.owner,
        summary=summary,
        content=f"{summary} full text",
        type="fact",
        scope="persistent",
        client="test",
        importance=0.7,
        embedding_status="success",
        tags=[],
        details={"resource_id": w.slugs[key], "doc_id": doc_id},
        **kw,
    )


async def _publish(w: World, key: str, context_id=None, resource_pk=None) -> None:
    async with w.factory() as s:
        s.add(
            PublicMcpEndpoint(
                workspace_id=w.ws,
                context_id=context_id or w.ctx[key],
                resource_pk=resource_pk or w.resource_pk[key],
                resource_id=w.slugs[key],
                public_name="Catalog",
                created_by=w.owner,
            )
        )
        await s.commit()


async def _map(w: World, key: str, *doc_ids: str, resource_pk=None) -> None:
    pk = resource_pk or w.resource_pk[key]
    async with w.factory() as s:
        for doc_id in doc_ids:
            s.add(
                PublicMcpDocKey(
                    resource_pk=pk,
                    doc_key=doc_key(pk, doc_id, get_settings().audit_hmac_key),
                    resource_doc_id=doc_id,
                )
            )
        await s.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def published(world: World) -> World:
    async with world.factory() as s:
        s.add_all(
            [
                _memory(
                    world, "a1", f"{DOC_MARKER}-1", LONG_SUMMARY, source_uri="https://ex.test/w1"
                ),
                _memory(world, "a1", f"{DOC_MARKER}-2", "Red gadget", source_uri="s3://bucket/x"),
                _memory(world, "a1", f"{DOC_MARKER}-3", "Gone item", deleted_at=utcnow()),
                _memory(world, "a2", f"{DOC_MARKER}-9", "Sibling item"),
            ]
        )
        await s.commit()
    await _publish(world, "a1")
    await _publish(world, "a2")
    await _map(world, "a1", f"{DOC_MARKER}-1", f"{DOC_MARKER}-2", f"{DOC_MARKER}-3")
    await _map(world, "a2", f"{DOC_MARKER}-9")
    return world


async def _resolve(w: World, secret: str, keys: list[str], resource: str | None = "a1"):
    body: dict[str, Any] = {"doc_keys": keys}
    if resource is not None:
        body["resource_id"] = w.slugs[resource]
    async with app_client(w) as c:
        return await c.post(BASE, headers=bearer(secret), json=body)


async def test_resolution_and_minimisation(published: World) -> None:
    w = published
    _, secret = await issue_credential(w)
    k1, k2, k3 = (_key(w, "a1", f"{DOC_MARKER}-{n}") for n in (1, 2, 3))
    sibling = _key(w, "a2", f"{DOC_MARKER}-9")
    unknown = "0" * 32
    r = await _resolve(w, secret, [k1, k2, k3, sibling, unknown])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["publication_serving"] is True
    assert body["schema_version"] == 1 and body["as_of"].endswith("Z")
    docs = body["documents"]
    assert docs[k1]["source_url"] == "https://ex.test/w1"
    assert docs[k1]["title"].startswith("Blue widget") and len(docs[k1]["title"]) <= 200
    assert docs[k2] == {"title": "Red gadget", "source_url": None}  # not an http(s) URL
    assert docs[k3] is None  # deleted memory
    assert docs[sibling] is None  # another resource's key: not in this mapping
    assert docs[unknown] is None
    for forbidden in (DOC_MARKER, "memory_id", '"details"', "doc_id", f"obs-a-{w.tag}"):
        assert forbidden not in r.text, forbidden


async def test_bounds_and_scope(published: World) -> None:
    w = published
    _, secret = await issue_credential(w)
    _, bound = await issue_credential(w, resource="a1")
    many = [f"{n:032x}" for n in range(101)]
    assert (await _resolve(w, secret, many)).status_code == 422
    assert (await _resolve(w, secret, ["NOT-HEX"])).status_code == 422
    assert (await _resolve(w, secret, [])).status_code == 422
    # A workspace credential must name the resource; a bound one may omit it.
    assert (await _resolve(w, secret, ["0" * 32], resource=None)).status_code == 422
    k1 = _key(w, "a1", f"{DOC_MARKER}-1")
    r = await _resolve(w, bound, [k1], resource=None)
    assert r.json()["documents"][k1] is not None
    # A resource-scoped credential on its sibling: 404, not null.
    r = await _resolve(w, bound, [_key(w, "a2", f"{DOC_MARKER}-9")], resource="a2")
    assert r.status_code == 404 and r.json()["error"] == "resource_not_found"
    # Another workspace's credential: 404.
    _, other = await issue_credential(w, workspace="b")
    assert (await _resolve(w, other, [k1])).status_code == 404
    r = await _resolve(w, secret, [k1])
    r2 = await _resolve(w, secret, [k1], resource="a1")
    assert r.json()["documents"] == r2.json()["documents"]
    async with app_client(w) as c:
        r = await c.post(
            BASE,
            headers=bearer(secret),
            json={"resource_id": w.slugs["a1"], "doc_keys": [k1], "schema_version": 2},
        )
        assert r.status_code == 400 and r.json()["error"] == "unsupported_schema_version"


async def test_capacity_lock_and_pause_are_503(published: World) -> None:
    w = published
    _, secret = await issue_credential(w)
    k1 = _key(w, "a1", f"{DOC_MARKER}-1")

    async def locked(*args, **kwargs):
        raise CapacityLockedError.for_outsider()

    with patch("api.routes.observability.ensure_not_capacity_locked", new=locked):
        r = await _resolve(w, secret, [k1])
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "300"
    assert r.json() == {
        "error": "temporarily_unavailable",
        "message": "The data is briefly unavailable. Try again later.",
        "details": {},
    }

    def paused(workspace):
        raise QuotaExceededError("paused", quota_type="api_public_daily")

    with patch("api.routes.observability.ensure_public_serving_allowed", new=paused):
        r = await _resolve(w, secret, [k1])
    assert r.status_code == 503


async def test_switch_off_unpublish_and_slug_reuse(published: World, monkeypatch) -> None:
    w = published
    _, secret = await issue_credential(w)
    k1 = _key(w, "a1", f"{DOC_MARKER}-1")
    assert (await _resolve(w, secret, [k1])).json()["documents"][k1] is not None

    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", False)
    body = (await _resolve(w, secret, [k1])).json()
    assert body == {**body, "publication_serving": False, "documents": {k1: None}}
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)

    # Unpublish: every key is null at once (nothing is cached).
    async with w.factory() as s:
        await s.execute(
            update(PublicMcpEndpoint)
            .where(PublicMcpEndpoint.context_id == w.ctx["a1"])
            .values(revoked_at=utcnow(), revoked_by=w.owner)
        )
        await s.commit()
    body = (await _resolve(w, secret, [k1])).json()
    assert body["publication_serving"] is False and body["documents"] == {k1: None}

    # Slug reuse: the resource is deleted and a new one takes its slug, with a
    # new context, the same document ids and a new publication. Old keys are
    # not in the new resource's mapping.
    async with w.factory() as s:
        await s.execute(
            update(Context).where(Context.id == w.ctx["a1"]).values(deleted_at=utcnow())
        )
        old = await s.get(Resource, w.resource_pk["a1"])
        assert old is not None
        await s.delete(old)
        await s.flush()
        new_res = Resource(id=uuid4(), workspace_id=w.ws, resource_id=w.slugs["a1"])
        new_ctx = Context(
            id=uuid4(),
            workspace_id=w.ws,
            name=f"{w.slugs['a1']}-new",
            created_by=w.owner,
            is_private=False,
            is_public=True,
            resource_id=w.slugs["a1"],
        )
        s.add_all([new_res, new_ctx])
        await s.flush()
        s.add(_memory(w, "a1", f"{DOC_MARKER}-1", "Reborn widget", context_id=new_ctx.id))
        await s.commit()
    await _publish(w, "a1", context_id=new_ctx.id, resource_pk=new_res.id)
    await _map(w, "a1", f"{DOC_MARKER}-1", resource_pk=new_res.id)
    new_key = doc_key(new_res.id, f"{DOC_MARKER}-1", get_settings().audit_hmac_key)
    body = (await _resolve(w, secret, [k1, new_key])).json()
    assert body["publication_serving"] is True
    assert body["documents"][k1] is None
    assert body["documents"][new_key]["title"] == "Reborn widget"
