"""#2043 PR2 against a real Postgres: the log reads back in order and an
earlier version rebuilds from its JSONB ``prior`` values (JSON null kept
distinct from an absent key), including after the erasure scrub left its
marker.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.auth import Context, Workspace
from models.memory import Memory, MemoryRevision
from services.account_erasure_service import AccountErasureService
from services.memory_versions import list_revision_rows, load_version

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def history(async_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """A memory at version 3 with its created + two updated events."""
    editor = f"revisions-it-{uuid.uuid4().hex[:8]}"
    ws_id, ctx_id, mem_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        session.add(Workspace(id=ws_id, name=f"ws-{editor}", owner_user_id=editor))
        await session.flush()
        session.add(Context(id=ctx_id, workspace_id=ws_id, name="revisions", created_by=editor))
        await session.flush()
        session.add(
            Memory(
                id=mem_id,
                user_id="the-author",
                workspace_id=ws_id,
                context_id=ctx_id,
                summary="third summary",
                context_summary="added in v2",
                content="body",
                type="note",
                importance=0.5,
                tags=["a", "b"],
                details={"k": "v3"},
                client="test",
                embedding_status="success",
                version=3,
            )
        )
        await session.flush()
        common = {"memory_id": mem_id, "context_id": ctx_id, "writer": "api"}
        session.add_all(
            [
                MemoryRevision(
                    **common,
                    version=1,
                    kind="created",
                    actor_user_id="the-author",
                    at=datetime(2026, 10, 1),
                ),
                MemoryRevision(
                    **common,
                    version=2,
                    kind="updated",
                    actor_user_id=editor,
                    prior={"summary": "first summary", "context_summary": None},
                    at=datetime(2026, 10, 2),
                ),
                MemoryRevision(
                    **common,
                    version=3,
                    kind="updated",
                    actor_user_id=editor,
                    prior={"summary": "second summary", "tags": ["a"], "details": {"k": "v1"}},
                    at=datetime(2026, 10, 3),
                ),
            ]
        )
        await session.commit()
    try:
        yield {"editor": editor, "memory_id": mem_id}
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()


async def _memory(session: AsyncSession, memory_id: uuid.UUID) -> Memory:
    return (await session.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()


async def test_the_log_pages_newest_first(async_engine: AsyncEngine, history) -> None:
    async with _maker(async_engine)() as session:
        first, more = await list_revision_rows(
            session, history["memory_id"], before_id=None, limit=2
        )
        rest, done = await list_revision_rows(
            session, history["memory_id"], before_id=first[-1].id, limit=2
        )
    assert [(r.version, r.kind) for r in first] == [(3, "updated"), (2, "updated")]
    assert more is True
    assert [(r.version, r.kind) for r in rest] == [(1, "created")]
    assert done is False


async def test_version_one_rebuilds_with_null_kept_as_null(
    async_engine: AsyncEngine, history
) -> None:
    async with _maker(async_engine)() as session:
        memory = await _memory(session, history["memory_id"])
        v1 = await load_version(session, memory, 1)
        v2 = await load_version(session, memory, 2)
    assert v1.values["summary"] == "first summary"
    assert v1.values["context_summary"] is None  # JSON null in prior: "was null"
    assert v1.values["tags"] == ["a"] and v1.values["details"] == {"k": "v1"}
    assert v2.values["summary"] == "second summary"
    assert v2.values["context_summary"] == "added in v2"
    assert v2.at == datetime(2026, 10, 2)


async def test_after_erasure_the_old_details_are_unavailable(
    async_engine: AsyncEngine, history
) -> None:
    async with _maker(async_engine)() as session:
        service = AccountErasureService.__new__(AccountErasureService)
        service.db = session
        with patch("services.account_erasure_service._audit_salt", return_value="salt"):
            await service._erase_memory_revisions(history["editor"])
        await session.commit()

    async with _maker(async_engine)() as session:
        memory = await _memory(session, history["memory_id"])
        v2 = await load_version(session, memory, 2)
    assert v2.values["details"] is None
    assert v2.unavailable == ("details",)
    assert v2.values["summary"] == "second summary"  # the text stays
