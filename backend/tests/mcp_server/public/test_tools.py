"""Public tool handlers and their projections, on fake rows (#2675).

The response key sets are asserted exactly: a field added to a projection is a
deliberate change to the public contract, never an accident.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from mcp_server.public import tools
from mcp_server.public.publication import ServingEndpoint
from mcp_server.public.tools import PublicArgumentError
from models.memory import Memory

ENDPOINT = ServingEndpoint(
    id=uuid4(),
    public_id="pmcp_" + "B" * 22,
    workspace_id=uuid4(),
    context_id=uuid4(),
    resource_pk=uuid4(),
    resource_id="catalog",
    public_name="Product catalog",
    public_description="Public product data.",
    public_usage_guide="Search by product name.",
    context_name="catalog",
    owner_user_id="owner-1",
)


def _memory(**over: Any) -> Memory:
    m = Memory(
        id=over.pop("id", uuid4()),
        workspace_id=ENDPOINT.workspace_id,
        context_id=ENDPOINT.context_id,
        user_id="owner-1",
        summary=over.pop("summary", "A product"),
        content=over.pop("content", "Full product text"),
        type="fact",
        importance=0.9,
        scope="persistent",
        client="resource-indexer",
        tags=over.pop("tags", ["shoes"]),
        details={"resource_id": "catalog", "doc_id": "sku-1", "secret": "internal"},
        source_uri=over.pop("source_uri", "https://example.test/p/1"),
        created_at=datetime(2026, 11, 1, 12, 0, 0),
        updated_at=over.pop("updated_at", datetime(2026, 11, 2, 12, 0, 0)),
    )
    m.resource_doc_id = over.pop("doc_key", "sku-1")
    m.observed_at = over.pop("observed_at", None)
    assert not over, over
    return m


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self):
        return iter(self.rows)

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


@pytest.fixture
def session(monkeypatch):
    db = MagicMock()
    db.statements = []
    db.queue: list[list[Any]] = []

    async def execute(stmt, *args, **kwargs):
        db.statements.append(stmt)
        return _Result(db.queue.pop(0) if db.queue else [])

    db.execute = execute

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(tools, "_read_session", _session)
    return db


def _sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": False}))


def _assert_predicate(stmt) -> None:
    sql = _sql(stmt)
    for fragment in (
        "memories.workspace_id = ",
        "memories.context_id = ",
        "memories.resource_id = ",
        "memories.deleted_at IS NULL",
    ):
        assert fragment in sql, (fragment, sql)


def _body(result) -> dict[str, Any]:
    return json.loads(result.content[0].text)


@pytest.fixture
def search(monkeypatch) -> AsyncMock:
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr("services.search_service.SearchService.hybrid_search", mock)
    return mock


# ----------------------------------------------------------------------------
# recall
# ----------------------------------------------------------------------------


RECALL_ITEM_KEYS = {
    "memory_id",
    "doc_key",
    "summary",
    "score",
    "type",
    "tags",
    "source_url",
    "created_at",
    "updated_at",
}


async def test_recall_projects_only_rows_the_predicate_returns(session, search) -> None:
    keep, dropped = _memory(), _memory(summary="private note")
    search.return_value = [
        {"id": str(dropped.id), "score": 0.99, "payload": {"summary": "private note"}},
        {"id": str(keep.id), "score": 0.81234, "payload": {"secret": "x"}},
    ]
    session.queue.append([keep])  # the predicate re-read drops the other id
    result = await tools.public_recall(ENDPOINT, {"query": "shoes"})
    body = _body(result)
    assert set(body) == {"status", "count", "results"}
    assert body["count"] == 1
    item = body["results"][0]
    assert set(item) == RECALL_ITEM_KEYS
    assert item == {
        "memory_id": str(keep.id),
        "doc_key": "sku-1",
        "summary": "A product",
        "score": 0.8123,
        "type": "fact",
        "tags": ["shoes"],
        "source_url": "https://example.test/p/1",
        "created_at": "2026-11-01T12:00:00Z",
        "updated_at": "2026-11-02T12:00:00Z",
    }
    assert "private note" not in result.content[0].text
    assert result.outcome == "success"
    assert result.result_count == 1
    _assert_predicate(session.statements[0])
    kwargs = search.await_args.kwargs
    assert kwargs["user_id"] == "owner-1"
    assert kwargs["workspace_id"] == str(ENDPOINT.workspace_id)
    assert kwargs["context_id"] == str(ENDPOINT.context_id)
    assert kwargs["use_rerank"] is False
    assert kwargs["k"] == 5
    assert kwargs["filters"] is None


async def test_recall_optional_fields_are_omitted(session, search) -> None:
    bare = _memory(tags=[], source_uri="file:///etc/passwd", updated_at=None)
    observed = _memory(source_uri="javascript:alert(1)", observed_at="2026-10-01T00:00:00Z")
    search.return_value = [
        {"id": str(bare.id), "score": 0.5},
        {"id": str(observed.id), "score": 0.4},
    ]
    session.queue.append([bare, observed])
    body = _body(await tools.public_recall(ENDPOINT, {"query": "q"}))
    first, second = body["results"]
    assert set(first) == {
        "memory_id",
        "doc_key",
        "summary",
        "score",
        "type",
        "created_at",
        "updated_at",
    }
    assert first["updated_at"] is None
    assert "source_url" not in second
    assert second["observed_at"] == "2026-10-01T00:00:00Z"


@pytest.mark.parametrize(
    ("uri", "shown"),
    [
        ("https://example.test/a", True),
        ("http://example.test/a?x=1", True),
        ("HTTPS://example.test/a", True),
        ("ftp://example.test/a", False),
        ("s3://bucket/key", False),
        ("/relative/path", False),
        ("https:///no-host", False),
        ("", False),
        (None, False),
    ],
)
def test_source_url_only_for_http(uri, shown) -> None:
    assert (tools._source_url(uri) is not None) is shown


async def test_recall_tags_filter_all_and_rechecked(session, search) -> None:
    m = _memory(tags=["shoes", "red"])
    search.return_value = [{"id": str(m.id), "score": 0.5}]
    session.queue.append([m])
    await tools.public_recall(ENDPOINT, {"query": "q", "tags": ["shoes", "red"], "k": 3})
    assert search.await_args.kwargs["filters"] == {"tags": ["shoes", "red"], "tags_match": "all"}
    assert search.await_args.kwargs["k"] == 3
    assert "memories.tags @> " in _sql(session.statements[0])


async def test_recall_cuts_at_max_chars(session, search) -> None:
    rows = [_memory(summary="s" * 4_000) for _ in range(5)]
    search.return_value = [{"id": str(m.id), "score": 0.9 - i / 10} for i, m in enumerate(rows)]
    session.queue.append(rows)
    body = _body(await tools.public_recall(ENDPOINT, {"query": "q", "max_chars": 10_000}))
    assert body["truncated"] is True
    assert body["count"] == len(body["results"]) == 2
    assert [r["memory_id"] for r in body["results"]] == [str(rows[0].id), str(rows[1].id)]


async def test_recall_reports_degradation_without_detail(session, search) -> None:
    m = _memory()

    async def degraded(**kwargs):
        kwargs["degradation"].update(
            degraded=True, reason="embedding_unavailable", detail="provider key sk-123 rejected"
        )
        return [{"id": str(m.id), "score": 0.3}]

    search.side_effect = degraded
    session.queue.append([m])
    result = await tools.public_recall(ENDPOINT, {"query": "q"})
    body = _body(result)
    assert body["degraded"] is True
    assert body["degraded_reason"] == "embedding_unavailable"
    assert "sk-123" not in result.content[0].text
    assert result.outcome == "degraded"


async def test_recall_with_no_hits_reads_nothing(session, search) -> None:
    body = _body(await tools.public_recall(ENDPOINT, {"query": "q"}))
    assert body == {"status": "success", "count": 0, "results": []}
    assert session.statements == []


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "q" * 501},
        {"query": 7},
        {"query": "q", "k": 21},
        {"query": "q", "k": 0},
        {"query": "q", "k": True},
        {"query": "q", "search_mode": "rerank"},
        {"query": "q", "tags": ["t"] * 11},
        {"query": "q", "tags": ["t" * 101]},
        {"query": "q", "tags": "shoes"},
        {"query": "q", "max_chars": 9_999},
        {"query": "q", "max_chars": 40_001},
    ],
)
async def test_recall_caps(args, session, search) -> None:
    with pytest.raises(PublicArgumentError):
        await tools.public_recall(ENDPOINT, args)
    search.assert_not_awaited()


# ----------------------------------------------------------------------------
# reference
# ----------------------------------------------------------------------------


async def test_reference_projection_is_exact(session) -> None:
    m = _memory(content="hello world")
    session.queue.append([m])
    result = await tools.public_reference(ENDPOINT, {"memory_id": str(m.id)})
    body = _body(result)
    assert set(body) == {"status", "memory"}
    assert set(body["memory"]) == {
        "memory_id",
        "doc_key",
        "summary",
        "type",
        "tags",
        "source_url",
        "created_at",
        "updated_at",
        "content",
        "content_offset",
        "content_total_chars",
        "content_truncated",
        "content_next_offset",
    }
    assert body["memory"]["content"] == "hello world"
    assert body["memory"]["content_truncated"] is False
    assert "internal" not in result.content[0].text  # details never leave
    _assert_predicate(session.statements[0])
    assert result.result_count == 1
    assert result.continuation is False


async def test_reference_pages_content_and_continues(session) -> None:
    text = "".join(chr(ord("a") + i % 26) for i in range(30_000))
    m = _memory(content=text)
    session.queue.append([m])
    first = _body(await tools.public_reference(ENDPOINT, {"memory_id": str(m.id)}))["memory"]
    assert first["content_truncated"] is True
    nxt = first["content_next_offset"]
    assert 0 < nxt < len(text)
    assert first["content"] == text[:nxt]
    session.queue.append([m])
    second = await tools.public_reference(
        ENDPOINT, {"memory_id": str(m.id), "content_offset": nxt, "max_chars": 40_000}
    )
    page = _body(second)["memory"]
    assert page["content"] == text[nxt:]
    assert page["content_truncated"] is False
    assert second.continuation is True
    # Every page re-reads through the predicate.
    assert len(session.statements) == 2
    for stmt in session.statements:
        _assert_predicate(stmt)


async def test_reference_offset_past_the_end(session) -> None:
    m = _memory(content="short")
    session.queue.append([m])
    result = await tools.public_reference(ENDPOINT, {"memory_id": str(m.id), "content_offset": 6})
    body = _body(result)
    assert body["error"] == "invalid_argument"
    assert body["content_total_chars"] == 5
    assert result.outcome == "invalid_request"


async def test_reference_miss_is_one_message(session) -> None:
    unknown = uuid4()
    result = await tools.public_reference(
        ENDPOINT, {"memory_id": str(unknown), "content_offset": 5}
    )
    assert _body(result) == {
        "status": "error",
        "error": "memory_not_found",
        "message": f"Memory not found on this endpoint: {unknown}",
        "help": "Use a memory_id returned by recall or explore on this endpoint.",
    }
    assert getattr(result.content, "is_error", False) is True


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"memory_id": "not-a-uuid"},
        {"memory_id": 5},
        {"memory_id": str(UUID(int=1)), "content_offset": -1},
        {"memory_id": str(UUID(int=1)), "content_offset": "10"},
        {"memory_id": str(UUID(int=1)), "max_chars": 50_000},
    ],
)
async def test_reference_caps(args, session) -> None:
    with pytest.raises(PublicArgumentError):
        await tools.public_reference(ENDPOINT, args)
    assert session.statements == []


# ----------------------------------------------------------------------------
# explore
# ----------------------------------------------------------------------------


def _n(summary: str, edge_type: str = "co_activation") -> tools._Neighbour:
    return tools._Neighbour(uuid4(), f"doc-{summary}", summary, edge_type)


async def test_explore_projection_and_hops(session, monkeypatch) -> None:
    seed_id = uuid4()
    session.queue.append([SimpleNamespace(id=seed_id, resource_doc_id="doc-seed", summary="seed")])
    a, b, c = _n("a"), _n("b", "semantic_similarity"), _n("c")
    calls: list[tuple[list[UUID], list[UUID], int]] = []

    async def neighbours(db, endpoint, frontier, exclude, limit):
        calls.append((list(frontier), list(exclude), limit))
        return [a, b] if len(calls) == 1 else [c]

    monkeypatch.setattr(tools, "_neighbours", neighbours)
    result = await tools.public_explore(ENDPOINT, {"memory_id": str(seed_id), "depth": 2})
    body = _body(result)
    assert set(body) == {"status", "exploration"}
    assert set(body["exploration"]) == {"seed", "related", "returned"}
    assert body["exploration"]["seed"] == {
        "memory_id": str(seed_id),
        "doc_key": "doc-seed",
        "summary": "seed",
    }
    related = body["exploration"]["related"]
    assert [set(r) for r in related] == [
        {"memory_id", "doc_key", "summary", "hop", "edge_type"}
    ] * 3
    assert [(r["summary"], r["hop"]) for r in related] == [("a", 1), ("b", 1), ("c", 2)]
    assert related[1]["edge_type"] == "semantic_similarity"
    assert "weight" not in result.content[0].text
    # Hop 2 walks only from the hop-1 entries that passed the predicate.
    assert calls[0] == ([seed_id], [seed_id], 10)
    assert calls[1] == ([a.memory_id, b.memory_id], [seed_id, a.memory_id, b.memory_id], 8)
    _assert_predicate(session.statements[0])
    assert result.result_count == 3


async def test_explore_depth_one_does_not_walk_further(session, monkeypatch) -> None:
    seed_id = uuid4()
    session.queue.append([SimpleNamespace(id=seed_id, resource_doc_id=None, summary="seed")])
    hop = AsyncMock(return_value=[_n(str(i)) for i in range(4)])
    monkeypatch.setattr(tools, "_neighbours", hop)
    body = _body(await tools.public_explore(ENDPOINT, {"memory_id": str(seed_id), "limit": 3}))
    assert hop.await_count == 1
    assert body["exploration"]["returned"] == 3
    assert body["exploration"]["truncated"] is True


async def test_explore_unknown_seed(session, monkeypatch) -> None:
    hop = AsyncMock()
    monkeypatch.setattr(tools, "_neighbours", hop)
    missing = uuid4()
    result = await tools.public_explore(ENDPOINT, {"memory_id": str(missing)})
    assert _body(result)["error"] == "memory_not_found"
    hop.assert_not_awaited()


@pytest.mark.parametrize(
    "args",
    [
        {"memory_id": str(UUID(int=1)), "depth": 3},
        {"memory_id": str(UUID(int=1)), "depth": 0},
        {"memory_id": str(UUID(int=1)), "limit": 26},
        {"memory_id": str(UUID(int=1)), "limit": 101},
        {"memory_id": "x"},
    ],
)
async def test_explore_caps(args, session) -> None:
    with pytest.raises(PublicArgumentError):
        await tools.public_explore(ENDPOINT, args)


async def test_neighbour_query_applies_the_predicate_and_the_endpoint_edges() -> None:
    captured: list[Any] = []

    class _Db:
        async def execute(self, stmt):
            captured.append(stmt)
            return _Result([])

    await tools._neighbours(_Db(), ENDPOINT, [UUID(int=1)], [UUID(int=1)], 5)
    sql = _sql(captured[0])
    _assert_predicate(captured[0])
    assert "neural_memory_edges.workspace_id = " in sql
    assert "neural_memory_edges.context_id = " in sql
    assert "row_number() OVER (PARTITION BY" in sql
    assert "weight" not in sql.split("FROM", 1)[0].split("SELECT", 1)[1]


# ----------------------------------------------------------------------------
# list_tags
# ----------------------------------------------------------------------------


async def test_list_tags_projection_and_scope(session, monkeypatch) -> None:
    agg = AsyncMock(
        return_value=[
            {"tag": "shoes", "count": 3, "last_used_at": datetime(2026, 1, 1)},
            {"tag": "red", "count": 1, "last_used_at": None},
        ]
    )
    monkeypatch.setattr("services.context_service.aggregate_context_tags", agg)
    result = await tools.public_list_tags(ENDPOINT, {"limit": 1, "sort": "alpha"})
    body = _body(result)
    assert body == {
        "status": "success",
        "tags": [{"tag": "shoes", "count": 3}],
        "total": 1,
        "has_more": True,
    }
    kwargs = agg.await_args.kwargs
    assert kwargs["workspace_id"] == ENDPOINT.workspace_id
    assert kwargs["context_id"] == ENDPOINT.context_id
    assert kwargs["resource_id"] == "catalog"
    assert kwargs["limit"] == 2
    assert kwargs["sort"] == "alpha"
    assert kwargs["q"] is None


@pytest.mark.parametrize(
    "args",
    [
        {"limit": 101},
        {"limit": 0},
        {"min_count": 0},
        {"min_count": 10_001},
        {"sort": "random"},
        {"prefix": "p" * 101},
        {"prefix": 3},
        {"with_tags": ["t"] * 11},
    ],
)
async def test_list_tags_caps(args, session) -> None:
    with pytest.raises(PublicArgumentError):
        await tools.public_list_tags(ENDPOINT, args)


# ----------------------------------------------------------------------------
# get_context_info
# ----------------------------------------------------------------------------


async def test_get_context_info_is_the_approved_text_only() -> None:
    result = await tools.public_get_context_info(ENDPOINT, {})
    body = _body(result)
    assert set(body) == {"status", "endpoint", "resource", "context"}
    assert body["endpoint"] == {
        "public_id": ENDPOINT.public_id,
        "name": "Product catalog",
        "description": "Public product data.",
        "usage_guide": "Search by product name.",
    }
    assert body["resource"] == {"resource_id": "catalog"}
    assert body["context"] == {"id": str(ENDPOINT.context_id), "name": "catalog"}
    text = result.content[0].text.lower()
    for word in (
        "workspace",
        "stats",
        "search_config",
        "embedding",
        "guardrails",
        "instructions",
        "is_locked",
        "summary",
    ):
        assert word not in text, word


async def test_get_context_info_omits_empty_text() -> None:
    from dataclasses import replace

    bare = replace(ENDPOINT, public_description=None, public_usage_guide=None)
    body = _body(await tools.public_get_context_info(bare, {}))
    assert set(body["endpoint"]) == {"public_id", "name"}
