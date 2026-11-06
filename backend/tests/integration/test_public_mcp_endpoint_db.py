"""The anonymous public MCP endpoint end to end on real rows (#2675).

The FastAPI app over ``httpx.ASGITransport``; every session is on the test
database. The owner publishes over REST; anonymous clients then talk MCP on
``/mcp/public/<id>``. ``SearchService.hybrid_search`` is patched to return
EVERY memory id of the fixture, so the predicate re-read is what keeps the
foreign rows out. The minute buckets run on an in-memory counter.

Fixture rows around one published resource context P (slug ``cat``):

* ``p1`` .. ``p4``: resource rows of P (``details.resource_id`` = slug);
* ``a``: a memory of another workspace;
* ``b``: a memory of a private context of the same workspace;
* ``c``: a member's hand-written note in P (no ``details.resource_id``);
* ``d``: a deleted resource row of P;
* ``e1``: a resource row of a second published endpoint (another context);

edges (P's workspace and context): p1→p2 twice (two members, weights 0.4 /
0.9), p1→a, p1→b, p1→c, p1→d, c→p3 (p3 is reachable only through c),
p2→p4 (hop 2).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.auth import Context, User, Workspace, WorkspaceMember
from models.llm_call_log import LLMCallLog
from models.memory import Memory, NeuralMemoryEdge
from models.resource import Resource
from services.llm_call_log_writer import LLMCallLogWriter
from services.search_service import SearchService
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

MODERN = "2026-07-28"
NOT_PUBLISHED = {
    "error": "not_found",
    "error_description": "No public MCP endpoint is published at this address.",
}


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    owner: str
    member: str
    tag: str
    ws: dict[str, UUID] = field(default_factory=dict)
    ctx: dict[str, UUID] = field(default_factory=dict)
    mem: dict[str, UUID] = field(default_factory=dict)
    slug: dict[str, str] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)


def _workspace(owner: str, name: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=name,
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _memory(ws: UUID, ctx: UUID, author: str, summary: str, **kw: Any) -> Memory:
    slug = kw.pop("slug", None)
    doc = kw.pop("doc", None)
    details = {"resource_id": slug, "doc_id": doc} if slug else {"note": "hand-written"}
    return Memory(
        id=uuid4(),
        workspace_id=ws,
        context_id=ctx,
        user_id=author,
        summary=summary,
        content=kw.pop("content", f"{summary} full text"),
        type="fact",
        scope="persistent",
        client="test",
        importance=0.7,
        embedding_status="success",
        tags=kw.pop("tags", []),
        details=details,
        source_uri=kw.pop("source_uri", None),
        **kw,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, member, outsider = f"pmo-{tag}", f"pmm-{tag}", f"pmx-{tag}"
    w = World(factory=factory, owner=owner, member=member, tag=tag)
    w.slug = {"cat": f"cat-{tag}", "second": f"second-{tag}", "other": f"other-{tag}"}
    async with factory() as s:
        for uid in (owner, member, outsider):
            s.add(User(email=f"{uid}@example.test", user_id=uid, name=uid, role="user"))
        await s.flush()
        main, foreign = (
            _workspace(owner, f"Acme Secret WS {tag}"),
            _workspace(outsider, f"Foreign WS {tag}"),
        )
        s.add_all([main, foreign])
        await s.flush()
        w.ws = {"main": main.id, "foreign": foreign.id}
        w.names = [main.name, foreign.name]
        s.add_all(
            [
                WorkspaceMember(workspace_id=main.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=main.id, user_id=member, role=WorkspaceRole.MEMBER),
                WorkspaceMember(
                    workspace_id=foreign.id, user_id=outsider, role=WorkspaceRole.OWNER
                ),
                Resource(workspace_id=main.id, resource_id=w.slug["cat"], created_by=owner),
                Resource(workspace_id=main.id, resource_id=w.slug["second"], created_by=owner),
                Resource(workspace_id=foreign.id, resource_id=w.slug["other"], created_by=outsider),
            ]
        )

        def ctx(ws: UUID, name: str, *, public: bool, slug: str | None, by: str) -> Context:
            return Context(
                id=uuid4(),
                workspace_id=ws,
                name=name,
                created_by=by,
                is_private=not public and slug is None,
                is_public=public,
                resource_id=slug,
            )

        contexts = {
            "pub": ctx(main.id, w.slug["cat"], public=True, slug=w.slug["cat"], by=owner),
            "second": ctx(main.id, w.slug["second"], public=True, slug=w.slug["second"], by=owner),
            "private": ctx(main.id, f"private-{tag}", public=False, slug=None, by=member),
            "foreign": ctx(
                foreign.id, w.slug["other"], public=True, slug=w.slug["other"], by=outsider
            ),
        }
        s.add_all(list(contexts.values()))
        await s.flush()
        w.ctx = {k: v.id for k, v in contexts.items()}
        pub, cat = w.ctx["pub"], w.slug["cat"]
        mems = {
            "p1": _memory(
                main.id,
                pub,
                owner,
                "Red running shoe",
                slug=cat,
                doc="sku-1",
                tags=["shoes", "red"],
                source_uri="https://shop.example.test/p/1",
                content="R" * 30_000,
            ),
            "p2": _memory(
                main.id, pub, owner, "Blue running shoe", slug=cat, doc="sku-2", tags=["shoes"]
            ),
            "p3": _memory(main.id, pub, owner, "Green hat", slug=cat, doc="sku-3", tags=["hats"]),
            "p4": _memory(
                main.id,
                pub,
                owner,
                "Shoe laces",
                slug=cat,
                doc="sku-4",
                tags=["shoes"],
                source_uri="s3://internal-bucket/sku-4",
            ),
            "a": _memory(
                foreign.id,
                w.ctx["foreign"],
                outsider,
                "Foreign secret plan",
                slug=w.slug["other"],
                doc="x-1",
                tags=["foreign-only"],
            ),
            "b": _memory(
                main.id, w.ctx["private"], member, "Private salary note", tags=["private-only"]
            ),
            "c": _memory(main.id, pub, member, "Member draft pricing", tags=["draft-only"]),
            "d": _memory(
                main.id,
                pub,
                owner,
                "Discontinued shoe",
                slug=cat,
                doc="sku-9",
                tags=["deleted-only"],
                deleted_at=utcnow(),
            ),
            "e1": _memory(
                main.id,
                w.ctx["second"],
                owner,
                "Second dataset row",
                slug=w.slug["second"],
                doc="s-1",
                tags=["second-only"],
            ),
        }
        s.add_all(list(mems.values()))
        await s.flush()
        w.mem = {k: v.id for k, v in mems.items()}

        def edge(user: str, src: str, dst: str, weight: float, edge_type: str = "related_to"):
            return NeuralMemoryEdge(
                user_id=user,
                src_id=w.mem[src],
                dst_id=w.mem[dst],
                workspace_id=main.id,
                context_id=pub,
                edge_type=edge_type,
                weight=weight,
            )

        s.add_all(
            [
                edge(owner, "p1", "p2", 0.4),
                edge(member, "p1", "p2", 0.9, "neural_association"),
                edge(owner, "p1", "a", 2.5),
                edge(owner, "p1", "b", 2.5),
                edge(owner, "p1", "c", 2.5),
                edge(owner, "p1", "d", 2.5),
                edge(owner, "c", "p3", 2.5),
                edge(owner, "p2", "p4", 0.5),
            ]
        )
        await s.commit()
    try:
        yield w
    finally:
        async with factory() as s:
            ids = list(w.ws.values())
            for stmt in (
                delete(NeuralMemoryEdge).where(NeuralMemoryEdge.workspace_id.in_(ids)),
                delete(Memory).where(Memory.workspace_id.in_(ids)),
                delete(Workspace).where(Workspace.id.in_(ids)),
                delete(User).where(User.user_id.in_([owner, member, outsider])),
            ):
                await s.execute(stmt)
                await s.commit()


@dataclass
class Harness:
    world: World
    client: httpx.AsyncClient
    search: AsyncMock

    async def publish(self, slug_key: str = "cat", name: str = "Product catalog") -> str:
        r = await self.client.post(
            f"/api/v1/resources/{self.world.slug[slug_key]}/public-mcp",
            json={
                "public_name": name,
                "public_description": "Public product data.",
                "public_usage_guide": "Search by product name; use reference for details.",
            },
            headers={"x-test-user": self.world.owner},
        )
        assert r.status_code == 201, r.text
        assert r.json()["serving"] is True
        return r.json()["public_id"]

    async def unpublish(self, slug_key: str = "cat") -> None:
        r = await self.client.delete(
            f"/api/v1/resources/{self.world.slug[slug_key]}/public-mcp",
            headers={"x-test-user": self.world.owner},
        )
        assert r.status_code == 204, r.text

    async def rpc(
        self,
        pid: str,
        method: str,
        params: dict | None = None,
        *,
        session: str | None = None,
        query: str = "",
    ) -> httpx.Response:
        headers = {"mcp-session-id": session} if session else {}
        return await self.client.post(
            f"/mcp/public/{pid}{query}",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            headers=headers,
        )

    async def initialize(self, pid: str) -> str:
        r = await self.rpc(pid, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
        assert r.status_code == 200, r.text
        return r.headers["mcp-session-id"]

    async def call(
        self, pid: str, session: str, tool: str, /, **arguments: Any
    ) -> tuple[dict, bool]:
        r = await self.rpc(
            pid, "tools/call", {"name": tool, "arguments": arguments}, session=session
        )
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        return json.loads(result["content"][0]["text"]), bool(result.get("isError"))

    async def modern_call(self, pid: str, tool: str, /, **arguments: Any) -> httpx.Response:
        return await self.client.post(
            f"/mcp/public/{pid}",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": tool,
                    "arguments": arguments,
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": MODERN,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
            headers={"mcp-protocol-version": MODERN, "mcp-method": "tools/call", "mcp-name": tool},
        )


@pytest_asyncio.fixture(loop_scope="session")
async def harness(world: World, monkeypatch) -> AsyncIterator[Harness]:

    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db as real_get_db

    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with world.factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def _user(request: Request) -> dict[str, Any]:
        uid = request.headers.get("x-test-user")
        if not uid:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return {
            "user_id": uid,
            "email": f"{uid}@example.test",
            "role": "user",
            "current_workspace_id": world.ws["main"],
        }

    counts: dict[str, int] = {}

    async def incr(key: str, amount: int, ttl: int | None = None) -> int:
        counts[key] = counts.get(key, 0) + amount
        return counts[key]

    every_id = list(world.mem.values())

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"id": str(mid), "score": 0.9 - i / 100, "payload": {}}
            for i, mid in enumerate(every_id)
        ]

    search_mock = AsyncMock(side_effect=search)
    app.dependency_overrides[real_get_db] = _db
    app.dependency_overrides[get_user_from_api_key_or_session] = _user
    try:
        with (
            patch("db.base.get_db", new=_db),
            patch("mcp_server.public.limits.incrby_counter", new=incr),
            patch("services.search_service.SearchService.hybrid_search", new=search_mock),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                yield Harness(world, client, search_mock)
    finally:
        app.dependency_overrides.pop(real_get_db, None)
        app.dependency_overrides.pop(get_user_from_api_key_or_session, None)


def _forbidden_strings(w: World) -> list[str]:
    hidden = [str(w.mem[k]) for k in ("a", "b", "c", "d", "e1")]
    return [
        *hidden,
        *w.names,
        str(w.ws["main"]),
        str(w.ws["foreign"]),
        str(w.ctx["private"]),
        str(w.ctx["second"]),
        str(w.ctx["foreign"]),
        "Foreign secret plan",
        "Private salary note",
        "Member draft pricing",
        "Discontinued shoe",
        "Second dataset row",
        "foreign-only",
        "private-only",
        "draft-only",
        "deleted-only",
        "second-only",
    ]


def _assert_clean(w: World, text: str, *, allow: tuple[str, ...] = ()) -> None:
    for value in _forbidden_strings(w):
        if value in allow:
            continue
        assert value not in text, value


# ----------------------------------------------------------------------------
# AC1 / AC4 / AC6
# ----------------------------------------------------------------------------


async def test_public_data_only_through_every_tool(harness: Harness) -> None:
    w = harness.world
    pid = await harness.publish()
    second_pid = await harness.publish("second", "Second dataset")
    assert second_pid != pid
    session = await harness.initialize(pid)

    r = await harness.rpc(pid, "tools/list", session=session)
    assert [t["name"] for t in r.json()["result"]["tools"]] == [
        "recall",
        "reference",
        "explore",
        "list_tags",
        "get_context_info",
    ]

    # get_context_info: the approved text and identifiers only.
    info, is_error = await harness.call(pid, session, "get_context_info")
    assert not is_error
    assert set(info) == {"status", "endpoint", "resource", "context"}
    assert info["endpoint"] == {
        "public_id": pid,
        "name": "Product catalog",
        "description": "Public product data.",
        "usage_guide": "Search by product name; use reference for details.",
    }
    assert info["resource"] == {"resource_id": w.slug["cat"]}
    assert info["context"] == {"id": str(w.ctx["pub"]), "name": w.slug["cat"]}
    lowered = json.dumps(info).lower()
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
        assert word not in lowered, word

    # recall: the search returns every id; only P's resource rows come back.
    body, is_error = await harness.call(pid, session, "recall", query="shoe", k=20)
    assert not is_error
    ids = [r["memory_id"] for r in body["results"]]
    assert set(ids) == {str(w.mem[k]) for k in ("p1", "p2", "p3", "p4")}
    by_id = {r["memory_id"]: r for r in body["results"]}
    assert by_id[str(w.mem["p1"])]["source_url"] == "https://shop.example.test/p/1"
    assert by_id[str(w.mem["p1"])]["doc_id"] == "sku-1"
    assert "source_url" not in by_id[str(w.mem["p4"])]  # s3:// is not public
    kwargs = harness.search.await_args.kwargs
    assert kwargs["user_id"] == w.owner
    assert kwargs["context_id"] == str(w.ctx["pub"])
    assert kwargs["use_rerank"] is False
    _assert_clean(w, json.dumps(body))

    # reference: every non-public id answers the same text.
    for key in ("a", "b", "c", "d", "e1"):
        body, is_error = await harness.call(pid, session, "reference", memory_id=str(w.mem[key]))
        assert is_error
        assert body == {
            "status": "error",
            "error": "memory_not_found",
            "message": f"Memory not found on this endpoint: {w.mem[key]}",
            "help": "Use a memory_id returned by recall or explore on this endpoint.",
        }
        _assert_clean(w, json.dumps(body), allow=(str(w.mem[key]),))
    random_id = uuid4()
    body, _ = await harness.call(pid, session, "reference", memory_id=str(random_id))
    assert body["message"] == f"Memory not found on this endpoint: {random_id}"

    # reference paging on a public row, continuation re-authorised.
    body, is_error = await harness.call(pid, session, "reference", memory_id=str(w.mem["p1"]))
    assert not is_error
    memory = body["memory"]
    assert "details" not in memory and "importance" not in memory and "version" not in memory
    assert memory["content_truncated"] is True
    nxt = memory["content_next_offset"]
    body, is_error = await harness.call(
        pid, session, "reference", memory_id=str(w.mem["p1"]), content_offset=nxt
    )
    assert not is_error and body["memory"]["content_offset"] == nxt
    body, is_error = await harness.call(
        pid, session, "reference", memory_id=str(w.mem["p1"]), content_offset=30_001
    )
    assert is_error and body["error"] == "invalid_argument"
    body, is_error = await harness.call(
        pid, session, "reference", memory_id=str(w.mem["c"]), content_offset=nxt
    )
    assert is_error and body["error"] == "memory_not_found"
    body, is_error = await harness.call(
        pid, session, "reference", memory_id=str(w.mem["p1"]), version=1
    )
    assert body == {
        "status": "error",
        "error": "invalid_argument",
        "message": "historical versions are not available on this endpoint",
    }

    # explore: one row per neighbour, no weight, no relay through c.
    body, is_error = await harness.call(
        pid, session, "explore", memory_id=str(w.mem["p1"]), depth=2
    )
    assert not is_error
    related = body["exploration"]["related"]
    assert [(r["memory_id"], r["hop"]) for r in related] == [
        (str(w.mem["p2"]), 1),
        (str(w.mem["p4"]), 2),
    ]
    assert related[0]["edge_type"] == "neural_association"  # the strongest of the two edges
    assert all(set(r) == {"memory_id", "doc_id", "summary", "hop", "edge_type"} for r in related)
    assert str(w.mem["p3"]) not in json.dumps(body)
    _assert_clean(w, json.dumps(body))
    body, is_error = await harness.call(pid, session, "explore", memory_id=str(w.mem["c"]))
    assert is_error and body["error"] == "memory_not_found"

    # list_tags: tags carried only by hidden rows are absent.
    body, is_error = await harness.call(pid, session, "list_tags")
    assert not is_error
    assert {t["tag"]: t["count"] for t in body["tags"]} == {"shoes": 3, "red": 1, "hats": 1}
    assert all(set(t) == {"tag", "count"} for t in body["tags"])

    # Other contexts and widening arguments.
    for other in (w.ctx["second"], w.ctx["private"], w.ctx["foreign"]):
        body, is_error = await harness.call(
            pid, session, "recall", query="x", context_id=str(other)
        )
        assert is_error
        assert body == {
            "status": "error",
            "error": "context_not_available",
            "message": "This endpoint serves one published dataset; omit context_id.",
        }
    body, _ = await harness.call(pid, session, "recall", query="x", context_id=str(w.ctx["pub"]))
    assert body["status"] == "success"
    body, is_error = await harness.call(
        pid, session, "recall", query="x", context_ids=[str(w.ctx["second"])]
    )
    assert is_error and body["error"] == "invalid_argument"
    _assert_clean(w, json.dumps(body))

    # The second endpoint serves only its own rows.
    s2 = await harness.initialize(second_pid)
    body, _ = await harness.call(second_pid, s2, "recall", query="row")
    assert [r["memory_id"] for r in body["results"]] == [str(w.mem["e1"])]
    body, is_error = await harness.call(second_pid, s2, "reference", memory_id=str(w.mem["p1"]))
    assert is_error and body["error"] == "memory_not_found"

    # A session of one endpoint cannot be used on another.
    r = await harness.rpc(second_pid, "tools/list", session=session)
    assert r.status_code == 404

    await harness.unpublish()
    await harness.unpublish("second")


# ----------------------------------------------------------------------------
# AC2: denied before the handler, nothing changes
# ----------------------------------------------------------------------------


async def test_write_and_admin_tools_change_nothing(harness: Harness) -> None:
    w = harness.world
    pid = await harness.publish()
    session = await harness.initialize(pid)

    async def snapshot() -> tuple[int, tuple]:
        async with w.factory() as s:
            count = (
                await s.execute(
                    select(func.count())
                    .select_from(Memory)
                    .where(Memory.workspace_id == w.ws["main"])
                )
            ).scalar_one()
            ctx = await s.get(Context, w.ctx["pub"])
            assert ctx is not None
            return count, (ctx.name, ctx.is_public, ctx.deleted_at, ctx.description)

    before = await snapshot()
    calls = [
        ("remember", {"summary": "injected", "content": "x", "context_id": str(w.ctx["pub"])}),
        ("forget", {"memory_id": str(w.mem["p1"])}),
        ("delete_context", {"context_id": str(w.ctx["pub"])}),
        ("update_context", {"context_id": str(w.ctx["pub"]), "description": "pwned"}),
        ("setup_resource", {"resource_id": "x"}),
        ("secret_get", {"name": "x"}),
        ("list_contexts", {}),
    ]
    for name, arguments in calls:
        body, is_error = await harness.call(pid, session, name, **arguments)
        assert is_error
        assert body == {
            "status": "error",
            "error": "unknown_tool",
            "message": f"Unknown tool: {name}",
        }
        r = await harness.modern_call(pid, name, **arguments)
        assert json.loads(r.json()["result"]["content"][0]["text"])["error"] == "unknown_tool"
    assert await snapshot() == before
    async with w.factory() as s:
        p1 = await s.get(Memory, w.mem["p1"])
        assert p1 is not None and p1.deleted_at is None
    await harness.unpublish()


async def test_public_reads_write_no_statistics(harness: Harness) -> None:
    w = harness.world
    pid = await harness.publish()
    session = await harness.initialize(pid)

    async def stats() -> list[tuple]:
        async with w.factory() as s:
            rows = (
                await s.execute(
                    select(
                        Memory.id, Memory.access_count, Memory.reference_count, Memory.last_used_at
                    )
                    .where(Memory.workspace_id == w.ws["main"])
                    .order_by(Memory.id)
                )
            ).all()
            edges = (
                await s.execute(
                    select(NeuralMemoryEdge.id, NeuralMemoryEdge.weight)
                    .where(NeuralMemoryEdge.workspace_id == w.ws["main"])
                    .order_by(NeuralMemoryEdge.id)
                )
            ).all()
            ctx = await s.get(Context, w.ctx["pub"])
            assert ctx is not None
            return [tuple(r) for r in rows] + [tuple(e) for e in edges] + [(ctx.last_used_at,)]

    async def search(self: SearchService, **kwargs: Any) -> list[dict[str, Any]]:
        # What the real search does on its session: one embedding cost row.
        await LLMCallLogWriter(self.db).record(
            caller="recall",
            call_type="embedding",
            provider="test",
            model="test-embed",
            user_id=kwargs["user_id"],
            workspace_id=kwargs["workspace_id"],
            context_id=kwargs["context_id"],
            embedding_tokens=7,
        )
        return [{"id": str(w.mem["p1"]), "score": 0.9, "payload": {}}]

    cost_rows = select(LLMCallLog.user_id, LLMCallLog.embedding_tokens).where(
        LLMCallLog.workspace_id == w.ws["main"]
    )
    before = await stats()
    with patch.object(SearchService, "hybrid_search", new=search):
        body, is_error = await harness.call(pid, session, "recall", query="shoe")
    assert not is_error and body["count"] == 1
    await harness.call(pid, session, "reference", memory_id=str(w.mem["p1"]))
    await harness.call(pid, session, "explore", memory_id=str(w.mem["p1"]), depth=2)
    await harness.call(pid, session, "list_tags")
    assert await stats() == before
    async with w.factory() as s:
        assert (await s.execute(cost_rows)).all() == [(w.owner, 7)]
        await s.execute(delete(LLMCallLog).where(LLMCallLog.workspace_id == w.ws["main"]))
        await s.commit()
    await harness.unpublish()


# ----------------------------------------------------------------------------
# AC5: withdrawal reaches open sessions and stateless callers
# ----------------------------------------------------------------------------


async def test_unpublish_reaches_an_open_session_and_republish_mints_a_new_id(
    harness: Harness,
) -> None:
    w = harness.world
    pid = await harness.publish()
    session = await harness.initialize(pid)
    body, is_error = await harness.call(pid, session, "recall", query="shoe")
    assert not is_error and body["count"] == 4

    await harness.unpublish()
    r = await harness.rpc(
        pid, "tools/call", {"name": "recall", "arguments": {"query": "shoe"}}, session=session
    )
    assert r.status_code == 404
    assert r.json() == NOT_PUBLISHED
    r = await harness.modern_call(pid, "recall", query="shoe")
    assert r.status_code == 404
    assert r.json() == NOT_PUBLISHED

    new_pid = await harness.publish()
    assert new_pid != pid
    r = await harness.rpc(pid, "initialize", {"protocolVersion": "2025-03-26"})
    assert r.status_code == 404
    new_session = await harness.initialize(new_pid)
    body, is_error = await harness.call(new_pid, new_session, "recall", query="shoe")
    assert not is_error and body["count"] == 4
    # The old session id is not valid on the new address either.
    r = await harness.rpc(new_pid, "tools/list", session=session)
    assert r.status_code == 404
    await harness.unpublish()
    _assert_clean(w, r.text)


@pytest.mark.parametrize("change", ["not_public", "slug_moved", "context_deleted"])
async def test_data_no_longer_public_closes_the_endpoint(harness: Harness, change: str) -> None:
    w = harness.world
    pid = await harness.publish()
    session = await harness.initialize(pid)
    body, is_error = await harness.call(pid, session, "get_context_info")
    assert not is_error

    values: dict[str, Any] = {
        # REST/MCP unpublish clears the slug with the flag (#156).
        "not_public": {"is_public": False, "resource_id": None},
        "slug_moved": {"resource_id": f"moved-{w.tag}"},
        "context_deleted": {"deleted_at": utcnow()},
    }[change]
    async with w.factory() as s:
        await s.execute(update(Context).where(Context.id == w.ctx["pub"]).values(**values))
        await s.commit()
    try:
        r = await harness.rpc(
            pid, "tools/call", {"name": "recall", "arguments": {"query": "x"}}, session=session
        )
        assert r.status_code == 404
        assert r.json() == NOT_PUBLISHED
        r = await harness.modern_call(pid, "get_context_info")
        assert r.status_code == 404
        r = await harness.client.get(
            f"/api/v1/resources/{w.slug['cat']}/public-mcp", headers={"x-test-user": w.owner}
        )
        assert r.status_code == 200 and r.json()["serving"] is False
    finally:
        async with w.factory() as s:
            await s.execute(
                update(Context)
                .where(Context.id == w.ctx["pub"])
                .values(is_public=True, resource_id=w.slug["cat"], deleted_at=None)
            )
            await s.commit()
        await harness.unpublish()


async def test_switch_off_closes_every_endpoint(harness: Harness, monkeypatch) -> None:
    pid = await harness.publish()
    session = await harness.initialize(pid)
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", False)
    r = await harness.rpc(pid, "tools/list", session=session)
    assert r.status_code == 404
    assert r.json() == NOT_PUBLISHED
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
    await harness.unpublish()
