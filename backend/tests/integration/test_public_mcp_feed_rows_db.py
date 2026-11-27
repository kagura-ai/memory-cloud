"""Feed rows on the public surfaces, on real rows (#3032).

The SQL gate (``feed_content_column``) and the loaded-row gate
(``feed_record_of``) agree for every row; ``reference`` and ``explore`` run
their widened selects on PostgreSQL; the document resolver shows a feed
record's title and URL and reads one row per document.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from mcp_server.public import tools
from mcp_server.public.feed_record import (
    FEED_RECORD_MAX_BYTES,
    FEED_RECORD_SOURCE,
    FeedRecord,
    feed_content_column,
    feed_record_of,
    parse_feed_record,
)
from mcp_server.public.publication import load_serving_endpoint
from models.memory import Memory, NeuralMemoryEdge
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_usage import PublicMcpDocKey
from services import observability_documents
from services.observability_documents import resolve_doc_keys, resolve_public_documents
from services.observability_scope import ScopedResource
from services.public_mcp_usage_events import doc_key
from tests.observability_helpers import World, world_rows
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

LABEL = "[catalog] sync:0f3a v1"


def _json(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False)


def _product(external_id: str, **over: Any) -> dict[str, Any]:
    return {
        "record_type": "product",
        "external_id": external_id,
        "title": f"Trail shoe {external_id}",
        "url": f"https://example.com/p/{external_id}",
        **over,
    }


def _sized(external_id: str, size: int) -> str:
    base = _product(external_id, description="")
    room = size - len(_json(base).encode())
    content = _json(_product(external_id, description="x" * room))
    assert len(content.encode()) == size
    return content


FAQ = {
    "record_type": "faq",
    "external_id": "faq-1",
    "question": "How do I return a shoe?",
    "answer": "Within 30 days.",
    "url": "https://example.com/faq/returns",
}

# name -> (content, source, version, expected record)
ROWS: dict[str, tuple[str, str, int, FeedRecord | None]] = {
    "product": (
        _json(_product("p1", title="Trail shoe " + "long " * 80)),
        FEED_RECORD_SOURCE,
        1,
        FeedRecord("product", "Trail shoe " + ("long " * 80).strip(), "https://example.com/p/p1"),
    ),
    "faq": (_json(FAQ), FEED_RECORD_SOURCE, 1, FeedRecord("faq", FAQ["question"], FAQ["url"])),
    "edited": (_json(_product("p2")), FEED_RECORD_SOURCE, 2, None),
    "hand": (_json(_product("p3")), "mcp_remember", 1, None),
    "oversize": (_sized("p4", FEED_RECORD_MAX_BYTES + 5_000), FEED_RECORD_SOURCE, 1, None),
    "extra_key": (_json(_product("p5", color="red")), FEED_RECORD_SOURCE, 1, None),
    "plain": ("A plain resource text.", FEED_RECORD_SOURCE, 1, None),
    "exact": (
        _sized("p6", FEED_RECORD_MAX_BYTES),
        FEED_RECORD_SOURCE,
        1,
        FeedRecord("product", "Trail shoe p6", "https://example.com/p/p6"),
    ),
    "one_over": (_sized("p7", FEED_RECORD_MAX_BYTES + 1), FEED_RECORD_SOURCE, 1, None),
    "wide": (
        _json(_product("p8", description="é" * 16_500)),
        FEED_RECORD_SOURCE,
        1,
        None,  # under the size in characters, over it in bytes
    ),
    "http_url": (
        _json(_product("p9", url="http://example.com/p/p9")),
        FEED_RECORD_SOURCE,
        1,
        FeedRecord("product", "Trail shoe p9", None),
    ),
}


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    async with world_rows(async_engine) as w:
        yield w


@pytest.fixture(autouse=True)
def switches(monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)


def _row(w: World, name: str, content: str, source: str, version: int, **kw: Any) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=w.ws,
        context_id=w.ctx["a1"],
        user_id=w.owner,
        summary=kw.pop("summary", LABEL),
        content=content,
        type="resource_data",
        scope="persistent",
        client="resource-indexer",
        importance=0.5,
        embedding_status="success",
        tags=[],
        details={"resource_id": w.slugs["a1"], "doc_id": kw.pop("doc_id", f"doc-{name}")},
        source=source,
        version=version,
        source_uri=kw.pop("source_uri", None),
        **kw,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def published(world: World) -> dict[str, Any]:
    w = world
    ids: dict[str, UUID] = {}
    now = utcnow()
    async with w.factory() as s:
        rows = {name: _row(w, name, *spec[:3]) for name, spec in ROWS.items()}
        # One document with two rows: the newer one is what recall shows.
        rows["twin_old"] = _row(
            w,
            "twin",
            _json(_product("t-old", title="Old title")),
            FEED_RECORD_SOURCE,
            1,
            doc_id="doc-twin",
            updated_at=now - timedelta(days=2),
        )
        rows["twin_new"] = _row(
            w,
            "twin",
            _json(_product("t-new", title="New title")),
            FEED_RECORD_SOURCE,
            1,
            doc_id="doc-twin",
            updated_at=now,
        )
        rows["gone"] = _row(w, "gone", _json(_product("g")), FEED_RECORD_SOURCE, 1, deleted_at=now)
        s.add_all(rows.values())
        await s.flush()
        ids = {name: m.id for name, m in rows.items()}
        for dst, weight in (("faq", 0.9), ("plain", 0.5), ("edited", 0.3)):
            s.add(
                NeuralMemoryEdge(
                    user_id=w.owner,
                    src_id=ids["product"],
                    dst_id=ids[dst],
                    workspace_id=w.ws,
                    context_id=w.ctx["a1"],
                    edge_type="related_to",
                    weight=weight,
                )
            )
        endpoint = PublicMcpEndpoint(
            workspace_id=w.ws,
            context_id=w.ctx["a1"],
            resource_pk=w.resource_pk["a1"],
            resource_id=w.slugs["a1"],
            public_name="Catalog",
            created_by=w.owner,
        )
        s.add(endpoint)
        await s.flush()
        public_id = endpoint.public_id
        endpoint_id = endpoint.id
        key = get_settings().audit_hmac_key
        for doc in ("doc-product", "doc-faq", "doc-twin", "doc-gone", "doc-plain"):
            s.add(
                PublicMcpDocKey(
                    resource_pk=w.resource_pk["a1"],
                    doc_key=doc_key(w.resource_pk["a1"], doc, key),
                    resource_doc_id=doc,
                )
            )
        await s.commit()
    return {"world": w, "ids": ids, "public_id": public_id, "endpoint_id": endpoint_id}


@contextlib.asynccontextmanager
async def _sessions(w: World, monkeypatch) -> AsyncIterator[None]:
    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        async with w.factory() as s:
            yield s

    monkeypatch.setattr(tools, "_read_session", _session)
    yield


async def test_sql_gate_and_row_gate_agree_for_every_row(published) -> None:
    """Invariant: parse_feed_record(<SQL value>) == feed_record_of(<loaded row>)."""
    w: World = published["world"]
    ids: dict[str, UUID] = published["ids"]
    async with w.factory() as s:
        result = await s.execute(
            select(Memory, feed_content_column()).where(Memory.id.in_(list(ids.values())))
        )
        by_id = {m.id: (m, value) for m, value in result.all()}
    assert len(by_id) == len(ids)
    seen: dict[str, FeedRecord | None] = {}
    for name, memory_id in ids.items():
        memory, value = by_id[memory_id]
        assert parse_feed_record(value) == feed_record_of(memory), name
        seen[name] = feed_record_of(memory)
    for name, spec in ROWS.items():
        assert seen[name] == spec[3], name
    # The size gate is in the statement: a larger row's content is not read.
    assert by_id[ids["oversize"]][1] is None
    assert by_id[ids["one_over"]][1] is None
    assert by_id[ids["wide"]][1] is None
    assert by_id[ids["exact"]][1] is not None
    assert by_id[ids["edited"]][1] is None and by_id[ids["hand"]][1] is None


async def test_reference_and_explore_on_real_rows(published, monkeypatch) -> None:
    w: World = published["world"]
    ids: dict[str, UUID] = published["ids"]
    async with w.factory() as s:
        endpoint = await load_serving_endpoint(s, published["public_id"])
    assert endpoint is not None
    async with _sessions(w, monkeypatch):
        ref = await tools.public_reference(endpoint, {"memory_id": str(ids["faq"])})
        memory = json.loads(ref.content[0].text)["memory"]
        assert (memory["summary"], memory["type"], memory["source_url"]) == (
            FAQ["question"],
            "faq",
            FAQ["url"],
        )
        assert memory["content"] == _json(FAQ)

        edited = await tools.public_reference(endpoint, {"memory_id": str(ids["edited"])})
        head = json.loads(edited.content[0].text)["memory"]
        assert (head["summary"], head["type"]) == (LABEL, "resource_data")
        assert "source_url" not in head

        explored = await tools.public_explore(endpoint, {"memory_id": str(ids["product"])})
        exploration = json.loads(explored.content[0].text)["exploration"]
    seed_summary = exploration["seed"]["summary"]
    assert seed_summary.startswith("Trail shoe long") and len(seed_summary) <= 500
    assert [(r["doc_id"], r["summary"]) for r in exploration["related"]] == [
        ("doc-faq", FAQ["question"]),
        ("doc-plain", LABEL),
        ("doc-edited", LABEL),
    ]


async def _scoped(s: AsyncSession, w: World, endpoint_id: UUID) -> ScopedResource:
    return ScopedResource(
        resource_pk=w.resource_pk["a1"],
        resource_id=w.slugs["a1"],
        name=None,
        created_at=utcnow(),
        workspace_id=w.ws,
        context_id=w.ctx["a1"],
        endpoint=await s.get(PublicMcpEndpoint, endpoint_id),
    )


async def test_resolver_follows_recall_and_reads_one_row_per_document(
    published, monkeypatch
) -> None:
    w: World = published["world"]
    secret = get_settings().audit_hmac_key
    keys = {
        doc: doc_key(w.resource_pk["a1"], doc, secret)
        for doc in ("doc-product", "doc-faq", "doc-twin", "doc-gone", "doc-plain")
    }
    unknown = "0" * 32
    counts: list[int] = []
    original = AsyncSession.execute

    async def counting(self, statement, *args, **kwargs):
        result = await original(self, statement, *args, **kwargs)
        if "memories" in str(statement).split("FROM", 1)[-1].split("WHERE", 1)[0]:
            frozen = result.freeze()
            counts.append(len(frozen.data))
            return frozen()
        return result

    async with w.factory() as s:
        scoped = await _scoped(s, w, published["endpoint_id"])
        monkeypatch.setattr(AsyncSession, "execute", counting)
        resolved = await resolve_doc_keys(s, scoped, [*keys.values(), unknown])
    assert resolved.publication_serving is True
    docs = resolved.documents
    product = docs[keys["doc-product"]]
    assert product is not None
    assert product["title"].startswith("Trail shoe long")
    assert len(product["title"]) <= observability_documents.DOCUMENT_TITLE_MAX_CHARS
    assert product["title"].endswith("…")
    assert product["source_url"] == "https://example.com/p/p1"
    assert docs[keys["doc-faq"]] == {"title": FAQ["question"], "source_url": FAQ["url"]}
    assert docs[keys["doc-twin"]] == {
        "title": "New title",
        "source_url": "https://example.com/p/t-new",
    }
    assert docs[keys["doc-plain"]] == {"title": LABEL, "source_url": None}
    assert docs[keys["doc-gone"]] is None
    assert docs[unknown] is None
    # The memory statement returned one row per document (doc-twin has two rows).
    assert counts == [4]


async def test_core_resolver_equals_the_wrapper(published) -> None:
    w: World = published["world"]
    key = doc_key(w.resource_pk["a1"], "doc-faq", get_settings().audit_hmac_key)
    async with w.factory() as s:
        endpoint = await load_serving_endpoint(s, published["public_id"])
        assert endpoint is not None
        core = await resolve_public_documents(s, endpoint, [key])
        wrapped = await resolve_doc_keys(s, await _scoped(s, w, published["endpoint_id"]), [key])
    assert core == wrapped.documents == {key: {"title": FAQ["question"], "source_url": FAQ["url"]}}
