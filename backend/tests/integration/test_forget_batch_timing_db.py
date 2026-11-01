"""A healthy 50-id ``forget(memory_ids=[...])`` fits the ``forget`` budget (#2201).

The MCP handler runs ``MemoryService.forget`` once per id against Postgres and
a live Qdrant: context gate, row permission, row update, ``forgotten``
revision, neural edge delete, commit, then the search point delete. The whole
call shares one budget, the ``forget`` timeout (``MCP_TIMEOUT_FORGET``, default
15 s); an id that does not fit comes back ``not_attempted``. Each of the 50
memories has a summary point, two content chunk points and two edges.

The bound asserted is the budget itself (every id ``deleted``, none
``not_attempted``) — measured locally the batch takes well under a tenth of
it, so a slow CI runner does not flake it. Skipped when Postgres (``db_session``)
or ``QDRANT_URL`` is unreachable; CI's integration job has both::

    make test-integration
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from db import qdrant as qdrant_module
from db.qdrant import add_memory_to_qdrant, ensure_kagura_memories_collection, upsert_chunk_points
from utils.content_chunks import build_chunk_payload, chunk_point_id

pytestmark = pytest.mark.asyncio(loop_scope="session")

BATCH = 50
_DIM = 8
_VEC = [1.0, 0, 0, 0, 0, 0, 0, 0]


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://localhost:6333")


@dataclass
class World:
    workspace_id: uuid.UUID
    context_id: uuid.UUID
    owner: str
    session: AsyncSession
    collection: str
    qdrant: AsyncQdrantClient


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine: AsyncEngine, monkeypatch) -> AsyncIterator[World]:
    from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole

    client = AsyncQdrantClient(url=_qdrant_url(), timeout=10)
    try:
        await client.get_collections()
    except Exception as exc:  # noqa: BLE001 — intentional broad skip guard
        await client.close()
        pytest.skip(f"Qdrant unreachable at {_qdrant_url()}: {exc}")
    monkeypatch.setattr(qdrant_module, "_qdrant_client", client)
    monkeypatch.setattr(qdrant_module, "_active_store", lambda: None)
    collection = f"kagura_memories_it_{uuid.uuid4().hex[:12]}"
    await ensure_kagura_memories_collection(_DIM, collection)

    tag = uuid.uuid4().hex[:8]
    owner = f"owner-{tag}"
    ws_id, ctx_id = uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
        await session.flush()
        session.add(
            Workspace(
                id=ws_id,
                name=f"ws-{tag}",
                plan_name="free",
                owner_user_id=owner,
                daily_api_limit=5000,
                weekly_api_limit=25000,
            )
        )
        await session.flush()
        session.add(WorkspaceMember(workspace_id=ws_id, user_id=owner, role=WorkspaceRole.OWNER))
        session.add(
            Context(
                id=ctx_id,
                workspace_id=ws_id,
                name=f"forget-timing-{tag}",
                created_by=owner,
                is_private=False,
                trust_tier="trusted",
            )
        )
        await session.commit()
        try:
            yield World(ws_id, ctx_id, owner, session, collection, client)
        finally:
            await session.rollback()
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()
            await client.delete_collection(collection)
            await client.close()


async def _seed(world: World) -> list[uuid.UUID]:
    """``BATCH`` live memories, each with its points and two edges."""
    from models.memory import Memory
    from repositories.neural_edge import NeuralEdgeRepository

    ids = [uuid.uuid4() for _ in range(BATCH)]
    world.session.add_all(
        [
            Memory(
                id=mid,
                user_id=world.owner,
                workspace_id=world.workspace_id,
                context_id=world.context_id,
                summary=f"timed memory {i}",
                content="body",
                type="note",
                importance=0.5,
                client="test",
                embedding_status="success",
            )
            for i, mid in enumerate(ids)
        ]
    )
    await world.session.flush()
    edges = NeuralEdgeRepository(world.session)
    for i, mid in enumerate(ids):
        await edges.create_or_update_edge(
            world.owner,
            mid,
            ids[(i + 1) % BATCH],
            weight=0.5,
            workspace_id=str(world.workspace_id),
            context_id=str(world.context_id),
        )
    await world.session.commit()
    payload = {"summary": "s", "type": "note", "importance": 0.5, "tags": [], "scope": "context"}
    for mid in ids:
        await add_memory_to_qdrant(
            user_id=world.owner,
            memory_id=mid,
            vector=_VEC,
            payload=dict(payload),
            workspace_id=str(world.workspace_id),
            context_id=str(world.context_id),
            collection_name=world.collection,
        )
        await upsert_chunk_points(
            [
                (
                    chunk_point_id(mid, c),
                    _VEC,
                    build_chunk_payload(payload, memory_id=mid, chunk_index=c, chunk_text="t"),
                )
                for c in range(2)
            ],
            user_id=world.owner,
            workspace_id=str(world.workspace_id),
            context_id=str(world.context_id),
            collection_name=world.collection,
        )
    return ids


async def _forget(world: World, ids: list[uuid.UUID]) -> tuple[dict[str, Any], float]:
    from mcp_server.tools.memory import handle_forget

    async def _db() -> AsyncIterator[AsyncSession]:
        yield world.session

    with (
        patch("db.base.get_db", new=_db),
        patch(
            "services.memory_service.resolve_collection_name",
            AsyncMock(return_value=world.collection),
        ),
        patch("services.plan_seats.ensure_member_seated", AsyncMock()),
        patch("services.permission_service.ensure_member_seated", AsyncMock()),
        patch("services.context_service.ensure_member_seated", AsyncMock()),
    ):
        started = time.perf_counter()
        out = await handle_forget(
            {"context_id": str(world.context_id), "memory_ids": [str(i) for i in ids]},
            world.owner,
            world.workspace_id,
        )
        elapsed = time.perf_counter() - started
    return json.loads(out[0].text), elapsed


async def test_a_healthy_50_id_batch_fits_the_forget_budget(world: World):
    from mcp_server.tools._constants import get_tool_timeout
    from models.memory import Memory

    ids = await _seed(world)
    body, elapsed = await _forget(world, ids)

    assert body["status"] == "success", body
    assert body["deleted_count"] == BATCH
    assert all(r["status"] == "deleted" for r in body["results"]), body["results"]
    assert elapsed < get_tool_timeout("forget")

    live = (
        await world.session.execute(
            select(func.count()).where(Memory.id.in_(ids), Memory.deleted_at.is_(None))
        )
    ).scalar_one()
    assert live == 0
    left = await world.qdrant.retrieve(world.collection, ids=[str(i) for i in ids])
    assert left == []
    chunks = await world.qdrant.count(world.collection, exact=True)
    assert chunks.count == 0


async def test_a_cancel_at_the_point_delete_leaves_a_tombstone_not_a_blind_row(world: World):
    """The batch deadline cancels an id while its point delete stalls: the row
    is already committed as deleted (reported ``deleted``) and its point is
    left for the orphan sweep — never a live row without its vector."""
    import asyncio

    from models.memory import Memory

    ids = (await _seed(world))[:2]
    never = asyncio.Event()

    async def stall(*args: Any, **kwargs: Any) -> None:
        await never.wait()

    with (
        patch("services.memory_service.delete_memory_from_qdrant", stall),
        patch("mcp_server.tools.memory.get_tool_timeout", return_value=1.0),
    ):
        body, elapsed = await _forget(world, ids)

    assert elapsed < 3.0
    assert body["results"][0] == {"memory_id": str(ids[0]), "status": "deleted"}
    assert body["results"][1]["error"] == "not_attempted"
    rows = dict(
        (
            await world.session.execute(
                select(Memory.id, Memory.deleted_at).where(Memory.id.in_(ids))
            )
        ).all()
    )
    assert rows[ids[0]] is not None  # committed tombstone
    assert rows[ids[1]] is None  # not attempted: live, with its point
    left = {
        str(p.id) for p in await world.qdrant.retrieve(world.collection, ids=[str(i) for i in ids])
    }
    assert left == {str(ids[0]), str(ids[1])}
