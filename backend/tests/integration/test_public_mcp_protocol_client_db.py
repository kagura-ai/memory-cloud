"""A real MCP client against the public endpoint, in process (#2675).

The SDK's ``ClientSession`` over its Streamable HTTP client transport, with an
``httpx2.AsyncClient`` whose transport is the FastAPI app itself: the same
bytes a remote connector would send, without a network. Real rows on the
test database; the search service is patched to return every id.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import Memory
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource

pytestmark = pytest.mark.asyncio(loop_scope="session")

httpx2 = pytest.importorskip("httpx2")
if not hasattr(httpx2, "ASGITransport"):  # pragma: no cover - depends on the installed SDK
    pytest.skip("httpx2 has no ASGITransport", allow_module_level=True)


@pytest_asyncio.fixture(loop_scope="session")
async def published(async_engine) -> AsyncIterator[dict[str, Any]]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    owner, slug = f"pc-owner-{tag}", f"faq-{tag}"
    async with factory() as s:
        s.add(User(email=f"{owner}@example.test", user_id=owner, name=owner, role="user"))
        await s.flush()
        ws = Workspace(
            id=uuid4(),
            name=f"pc-{tag}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=50000,
            weekly_api_limit=250000,
        )
        s.add(ws)
        await s.flush()
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
        s.add_all(
            [
                WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER),
                resource,
                ctx,
            ]
        )
        await s.flush()
        faq = Memory(
            id=uuid4(),
            workspace_id=ws.id,
            context_id=ctx.id,
            user_id=owner,
            summary="How do I return an item?",
            content="Returns are accepted within 30 days.",
            type="fact",
            scope="persistent",
            client="test",
            importance=0.5,
            embedding_status="success",
            tags=["returns"],
            details={"resource_id": slug, "doc_id": "faq-1"},
            source_uri="https://help.example.test/returns",
        )
        endpoint = PublicMcpEndpoint(
            workspace_id=ws.id,
            context_id=ctx.id,
            resource_pk=resource.id,
            resource_id=slug,
            public_name="Help center FAQ",
            public_description="Answers to common questions.",
            created_by=owner,
        )
        s.add_all([faq, endpoint])
        await s.commit()
        data = {"factory": factory, "public_id": endpoint.public_id, "faq": faq.id, "slug": slug}
    try:
        yield data
    finally:
        async with factory() as s:
            await s.execute(delete(Workspace).where(Workspace.id == ws.id))
            await s.execute(delete(User).where(User.user_id == owner))
            await s.commit()


async def test_sdk_client_initializes_lists_and_calls(published, monkeypatch) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from api.main import app

    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
    factory = published["factory"]

    async def _db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    counts: dict[str, int] = {}

    async def incr(key: str, amount: int, ttl: int | None = None) -> int:
        counts[key] = counts.get(key, 0) + amount
        return counts[key]

    search = AsyncMock(return_value=[{"id": str(published["faq"]), "score": 0.77}])
    url = f"http://testserver/mcp/public/{published['public_id']}"
    with (
        patch("db.base.get_db", new=_db),
        patch("mcp_server.public.limits.incrby_counter", new=incr),
        patch("services.search_service.SearchService.hybrid_search", new=search),
    ):
        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
            ) as http_client,
            streamable_http_client(url, http_client=http_client) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            init = await session.initialize()
            assert init.server_info.name == "kagura-memory-cloud-public"
            assert "Help center FAQ" in (init.instructions or "")

            listed = await session.list_tools()
            names = [tool.name for tool in listed.tools]
            assert names == ["recall", "reference", "explore", "list_tags", "get_context_info"]
            assert all(
                tool.annotations and tool.annotations.read_only_hint for tool in listed.tools
            )

            info = await session.call_tool("get_context_info", {})
            assert not info.is_error
            payload = json.loads(info.content[0].text)
            assert payload["endpoint"]["name"] == "Help center FAQ"
            assert payload["resource"] == {"resource_id": published["slug"]}

            found = await session.call_tool("recall", {"query": "return policy"})
            assert not found.is_error
            payload = json.loads(found.content[0].text)
            assert [r["doc_key"] for r in payload["results"]] == ["faq-1"]
            assert payload["results"][0]["source_url"] == "https://help.example.test/returns"

            denied = await session.call_tool("remember", {"summary": "x"})
            assert denied.is_error
            assert json.loads(denied.content[0].text)["error"] == "unknown_tool"
