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
from services.details_provenance import details_key_digest
from services.memory_versions import (
    list_revision_rows,
    load_version,
    type_changes_above,
    types_at_versions,
)

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


async def test_erasing_an_editor_keeps_the_details_they_overwrote(
    async_engine: AsyncEngine, history
) -> None:
    """The editor's v3 prior holds the author's details, not the editor's: it
    stays, and no version reads as unavailable."""
    async with _maker(async_engine)() as session:
        service = AccountErasureService.__new__(AccountErasureService)
        service.db = session
        with patch("services.account_erasure_service._audit_salt", return_value="salt"):
            await service._erase_memory_revisions(history["editor"])
        await session.commit()

    async with _maker(async_engine)() as session:
        memory = await _memory(session, history["memory_id"])
        v2 = await load_version(session, memory, 2)
    assert v2.values["details"] == {"k": "v1"}
    assert v2.unavailable == ()


async def test_erasure_strips_the_details_the_subject_wrote_not_the_ones_they_overwrote(
    async_engine: AsyncEngine,
) -> None:
    """A's details sit in the prior of the NEXT details edit after A's (here
    D's v5), not in A's own row, whose prior is B's. Erasing A strips v5 and
    keeps v3; the versions that held A's details read without A's keys
    (#2275: per key, nothing else was in the value)."""
    subject = f"erased-{uuid.uuid4().hex[:8]}"
    ws_id, ctx_id, mem_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        session.add(Workspace(id=ws_id, name=f"ws-{subject}", owner_user_id="owner-c"))
        await session.flush()
        session.add(Context(id=ctx_id, workspace_id=ws_id, name="erasure", created_by="owner-c"))
        await session.flush()
        session.add(
            Memory(
                id=mem_id,
                user_id="author-c",
                workspace_id=ws_id,
                context_id=ctx_id,
                summary="d's summary",
                content="body",
                type="note",
                details={"d": 1},
                client="test",
                embedding_status="success",
                version=5,
            )
        )
        await session.flush()
        common = {"memory_id": mem_id, "context_id": ctx_id, "writer": "api", "kind": "updated"}
        session.add_all(
            [
                MemoryRevision(
                    **common, version=2, actor_user_id="member-b", prior={"details": {"c": 1}}
                ),
                MemoryRevision(
                    **common, version=3, actor_user_id=subject, prior={"details": {"b": 1}}
                ),
                MemoryRevision(
                    **common, version=4, actor_user_id="member-d", prior={"summary": "a's"}
                ),
                MemoryRevision(
                    **common, version=5, actor_user_id="member-d", prior={"details": {"a": 1}}
                ),
            ]
        )
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            service = AccountErasureService.__new__(AccountErasureService)
            service.db = session
            with patch("services.account_erasure_service._audit_salt", return_value="salt"):
                await service._erase_memory_revisions(subject)
            await session.commit()

        async with _maker(async_engine)() as session:
            priors = (
                await session.execute(
                    select(MemoryRevision.version, MemoryRevision.prior)
                    .where(MemoryRevision.memory_id == mem_id)
                    .order_by(MemoryRevision.version)
                )
            ).all()
            memory = await _memory(session, mem_id)
            rebuilt = {v: await load_version(session, memory, v) for v in (1, 2, 3, 4)}
        assert [tuple(p) for p in priors] == [
            (2, {"details": {"c": 1}}),
            (3, {"details": {"b": 1}}),  # B's value, kept
            (4, {"summary": "a's"}),
            (5, {"details": {}, "erased_details_keys": [details_key_digest("a")]}),  # A's key
        ]
        assert rebuilt[4].values["details"] == {} and rebuilt[4].unavailable == ()
        assert rebuilt[3].values["details"] == {} and rebuilt[3].unavailable == ()
        assert rebuilt[2].values["details"] == {"b": 1} and rebuilt[2].unavailable == ()
        assert rebuilt[1].values["details"] == {"c": 1}
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()


async def test_a_scrubbed_live_details_is_unavailable_in_older_versions(
    async_engine: AsyncEngine,
) -> None:
    """The subject authored the memory; another member edited only the summary.
    Erasure NULLs the live details, so version 1 cannot read it either."""
    subject = f"author-{uuid.uuid4().hex[:8]}"
    ws_id, ctx_id, mem_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        session.add(Workspace(id=ws_id, name=f"ws-{subject}", owner_user_id=subject))
        await session.flush()
        session.add(Context(id=ctx_id, workspace_id=ws_id, name="scrub", created_by=subject))
        await session.flush()
        session.add(
            Memory(
                id=mem_id,
                user_id=subject,
                workspace_id=ws_id,
                context_id=ctx_id,
                summary="second summary",
                content="body",
                type="note",
                details={"k": 1},
                client="test",
                embedding_status="success",
                version=2,
            )
        )
        await session.flush()
        session.add(
            MemoryRevision(
                memory_id=mem_id,
                context_id=ctx_id,
                writer="api",
                kind="updated",
                version=2,
                actor_user_id="member-b",
                prior={"summary": "first summary"},
            )
        )
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            service = AccountErasureService.__new__(AccountErasureService)
            service.db = session
            with patch("services.account_erasure_service._audit_salt", return_value="salt"):
                await service._erase_memory_revisions(subject)
                await service._scrub_surviving_memories(subject)
            await session.commit()

        async with _maker(async_engine)() as session:
            memory = await _memory(session, mem_id)
            v1 = await load_version(session, memory, 1)
        assert memory.details is None
        assert v1.values["summary"] == "first summary"
        assert v1.values["details"] is None and v1.unavailable == ("details",)
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()


async def test_the_type_projection_agrees_with_the_rebuild(async_engine: AsyncEngine) -> None:
    """#2169: ``type_changes_above`` reads only the edits that name ``type``
    (in ``prior`` or an erasure marker) from the lowest version up, and the
    types it yields are the ones ``load_version`` rebuilds."""
    ws_id, ctx_id, mem_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    owner = f"types-{uuid.uuid4().hex[:8]}"
    async with _maker(async_engine)() as session:
        session.add(Workspace(id=ws_id, name=f"ws-{owner}", owner_user_id=owner))
        await session.flush()
        session.add(Context(id=ctx_id, workspace_id=ws_id, name="types", created_by=owner))
        await session.flush()
        session.add(
            Memory(
                id=mem_id,
                user_id=owner,
                workspace_id=ws_id,
                context_id=ctx_id,
                summary="s5",
                content="body",
                type="note",
                client="test",
                embedding_status="success",
                version=5,
            )
        )
        await session.flush()
        common = {
            "memory_id": mem_id,
            "context_id": ctx_id,
            "writer": "api",
            "kind": "updated",
            "actor_user_id": owner,
        }
        session.add_all(
            [
                MemoryRevision(**common, version=2, prior={"type": "fact", "summary": "s1"}),
                MemoryRevision(**common, version=3, prior={"summary": "s2"}),
                MemoryRevision(**common, version=4, prior={"type": "decision"}),
                # never written today (erasure marks only details): an
                # unavailable type reads as None
                MemoryRevision(**common, version=5, prior={"erased": ["type"], "summary": "s4"}),
            ]
        )
        await session.commit()
    try:
        async with _maker(async_engine)() as session:
            memory = await _memory(session, mem_id)
            changes = await type_changes_above(session, mem_id, 3)
            everything = await type_changes_above(session, mem_id, 1)
            rebuilt = {
                v: (await load_version(session, memory, v)).values["type"] for v in range(1, 6)
            }
        assert sorted(changes) == [(4, "decision"), (5, None)]
        assert types_at_versions("note", everything, range(1, 6)) == rebuilt
        assert rebuilt == {1: "fact", 2: "decision", 3: "decision", 4: None, 5: "note"}
    finally:
        async with _maker(async_engine)() as session:
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()
