"""Sleep report tools authorize against the report's context, on real Postgres.

``get_sleep_report`` reads and ``rollback_sleep_run`` writes the report's
context, so both resolve that context like every other context tool, and the
rollback needs the editor or owner role there — the gate ``rollback_session``
applies. The workspace the MCP session points at plays no part. A context the
caller cannot reach answers exactly like a missing report.

Bob's run was recorded in context C of workspace W; Bob also owns a personal
workspace P. Every case runs the real resolvers and the real undo handlers;
only the vector store and the embedder are faked. Skipped when Postgres is
unreachable.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import ContextRole
from mcp_server.tools._helpers import set_mcp_key_workspace_scope
from models.auth import Context, ContextMember, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import DELETED_BY_SLEEP_ARCHIVE, Memory, NeuralMemoryEdge
from models.sleep import SleepAction, SleepReport
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class World:
    db: AsyncSession
    owner: str
    bob: str
    w: UUID
    p: UUID
    context_id: UUID
    report_id: UUID
    rescored_id: UUID
    archived_id: UUID
    edge_id: UUID


def _workspace(owner: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"sleep-authz-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )


async def _build(
    db: AsyncSession,
    *,
    bob_role: str | None,
    context_role: ContextRole | None = None,
    allowed_context: bool = True,
    private: bool = False,
    with_context: bool = True,
) -> World:
    """Owner's workspace W with context C, Bob's workspace P, and a completed
    run of Bob's in C that recorded an edge, an importance change and an
    archive."""
    tag = uuid4().hex[:8]
    owner, bob = f"sleep-authz-owner-{tag}", f"sleep-authz-bob-{tag}"
    for user in (owner, bob):
        db.add(User(email=f"{user}@test.example", user_id=user, name=user, role="user"))
    await db.flush()
    w, p = _workspace(owner), _workspace(bob)
    db.add_all([w, p])
    await db.flush()
    db.add(WorkspaceMember(workspace_id=w.id, user_id=owner, role=WorkspaceRole.OWNER))
    db.add(WorkspaceMember(workspace_id=p.id, user_id=bob, role=WorkspaceRole.OWNER))
    context = Context(
        id=uuid4(),
        workspace_id=w.id,
        name=f"c-{tag}",
        # A private context is open to its creator only: Bob's own.
        created_by=bob if private else owner,
        is_private=private,
    )
    db.add(context)
    await db.flush()
    if bob_role is not None:
        db.add(
            WorkspaceMember(
                workspace_id=w.id,
                user_id=bob,
                role=bob_role,
                allowed_context_ids=[context.id] if allowed_context else [],
            )
        )
    if context_role is not None:
        db.add(ContextMember(context_id=context.id, user_id=bob, role=context_role.value))

    def memory(summary: str, **fields: Any) -> Memory:
        return Memory(
            id=uuid4(),
            workspace_id=w.id,
            context_id=context.id,
            user_id=bob,
            summary=summary,
            content="body",
            type="note",
            scope="working",
            client="test",
            importance=fields.pop("importance", 0.5),
            embedding_status="success",
            **fields,
        )

    rescored = memory("rescored", importance=0.9)
    archived = memory(
        "archived",
        deleted_at=utcnow() - timedelta(days=1),
        deleted_by=DELETED_BY_SLEEP_ARCHIVE,
    )
    other = memory("other")
    db.add_all([rescored, archived, other])
    await db.flush()
    edge = NeuralMemoryEdge(
        user_id=bob,
        src_id=rescored.id,
        dst_id=other.id,
        workspace_id=w.id,
        context_id=context.id,
        edge_type="related_to",
        weight=0.8,
        confidence=0.9,
        origin="semantic",
    )
    db.add(edge)
    report = SleepReport(
        id=uuid4(),
        user_id=bob,
        workspace_id=w.id,
        context_id=context.id if with_context else None,
        status="completed",
    )
    db.add(report)
    await db.flush()
    db.add_all(
        [
            SleepAction(
                report_id=report.id,
                phase="edge_discovery",
                action_type="create_edge",
                memory_id=rescored.id,
                target_id=other.id,
                details={"edge_type": "related_to"},
            ),
            SleepAction(
                report_id=report.id,
                phase="importance_reeval",
                action_type="update_importance",
                memory_id=rescored.id,
                details={"old_importance": 0.4},
            ),
            SleepAction(
                report_id=report.id,
                phase="consolidation",
                action_type="archive",
                memory_id=archived.id,
                details={"mode": "tombstone"},
            ),
        ]
    )
    await db.commit()
    return World(
        db=db,
        owner=owner,
        bob=bob,
        w=w.id,
        p=p.id,
        context_id=context.id,
        report_id=report.id,
        rescored_id=rescored.id,
        archived_id=archived.id,
        edge_id=edge.id,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def worlds(db_session: AsyncSession) -> AsyncIterator[list[World]]:
    built: list[World] = []
    yield built
    set_mcp_key_workspace_scope(None)
    set_agent_scope(None)
    await db_session.rollback()
    for world in built:
        users = [world.owner, world.bob]
        await db_session.execute(
            delete(NeuralMemoryEdge).where(NeuralMemoryEdge.workspace_id == world.w)
        )
        await db_session.execute(delete(Memory).where(Memory.workspace_id == world.w))
        await db_session.execute(delete(SleepReport).where(SleepReport.user_id == world.bob))
        await db_session.execute(delete(Workspace).where(Workspace.id.in_([world.w, world.p])))
        await db_session.execute(delete(User).where(User.user_id.in_(users)))
    await db_session.commit()


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    """Vector store and embedder: the only things not real here."""
    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1])
    with (
        patch("services.embedding_service.EmbeddingService", return_value=embedder),
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()),
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock()),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch("mcp_server.tools.sleep._log_tool_usage", new=AsyncMock()),
    ):
        yield


async def _call(world: World, tool: str, session_workspace: UUID | None, report_id=None) -> str:
    from mcp_server.tools import sleep

    async def get_db():
        yield world.db

    handler = getattr(sleep, f"handle_{tool}")
    rid = str(report_id or world.report_id)
    with patch("db.base.get_db", new=get_db):
        (block,) = await handler({"report_id": rid}, world.bob, session_workspace)
    return block.text


async def _not_found_reply(world: World, tool: str) -> str:
    """What the tool answers for the same report id when no such report exists."""
    await world.db.execute(
        SleepReport.__table__.update()
        .where(SleepReport.id == world.report_id)
        .values(user_id=f"{world.bob}-elsewhere")
    )
    try:
        return await _call(world, tool, world.p)
    finally:
        await world.db.execute(
            SleepReport.__table__.update()
            .where(SleepReport.id == world.report_id)
            .values(user_id=world.bob)
        )
        await world.db.commit()


async def _untouched(world: World) -> None:
    world.db.expunge_all()
    rows = {
        m.id: m
        for m in (
            await world.db.execute(
                select(Memory).where(Memory.id.in_([world.rescored_id, world.archived_id]))
            )
        ).scalars()
    }
    assert rows[world.rescored_id].importance == pytest.approx(0.9)
    assert rows[world.archived_id].deleted_at is not None
    edge = await world.db.get(NeuralMemoryEdge, world.edge_id)
    assert edge is not None
    report = await world.db.get(SleepReport, world.report_id)
    assert report is not None and report.status == "completed"


async def _assert_hidden(world: World, *session_workspaces: UUID | None) -> None:
    """Both tools answer like a missing report, whatever the session points at."""
    for tool in ("get_sleep_report", "rollback_sleep_run"):
        expected = await _not_found_reply(world, tool)
        assert json.loads(expected)["error"] == "report_not_found"
        for ws in session_workspaces:
            assert await _call(world, tool, ws) == expected, (tool, ws)
    await _untouched(world)


async def test_viewer_cannot_roll_back_from_any_session_workspace(
    db_session: AsyncSession, worlds: list[World]
) -> None:
    world = await _build(db_session, bob_role=WorkspaceRole.VIEWER)
    worlds.append(world)
    for ws in (world.w, world.p, None):
        data = json.loads(await _call(world, "rollback_sleep_run", ws))
        assert data["error"] == "permission_denied", (ws, data)
        assert (data["your_role"], data["required_role"]) == ("viewer", "editor")
    await _untouched(world)
    # A viewer still reads the context, so the report stays readable.
    data = json.loads(await _call(world, "get_sleep_report", world.p))
    assert data["status"] == "success"


async def test_removed_member_gets_not_found(db_session: AsyncSession, worlds: list[World]) -> None:
    from services.workspace_service import WorkspaceService

    world = await _build(db_session, bob_role=WorkspaceRole.MEMBER, context_role=ContextRole.EDITOR)
    worlds.append(world)
    await WorkspaceService(db_session).remove_member(world.w, world.bob)
    await db_session.commit()
    # remove_member hands Bob's memories to the owner; put them back on Bob so
    # a rollback that got through would show on them, not only on the edge.
    await db_session.execute(
        Memory.__table__.update()
        .where(Memory.id.in_([world.rescored_id, world.archived_id]))
        .values(user_id=world.bob)
    )
    await db_session.commit()
    await _assert_hidden(world, world.p, None)


async def test_removed_creator_of_a_private_context_gets_not_found(
    db_session: AsyncSession, worlds: list[World]
) -> None:
    from services.workspace_service import WorkspaceService

    world = await _build(
        db_session,
        bob_role=WorkspaceRole.MEMBER,
        context_role=ContextRole.OWNER,
        private=True,
    )
    worlds.append(world)
    # Before removal, Bob reads and could roll back his own private context.
    data = json.loads(await _call(world, "get_sleep_report", world.p))
    assert data["status"] == "success"
    await WorkspaceService(db_session).remove_member(world.w, world.bob)
    await db_session.commit()
    await _assert_hidden(world, world.p, None)


async def test_member_whose_context_access_was_withdrawn_gets_not_found(
    db_session: AsyncSession, worlds: list[World]
) -> None:
    world = await _build(
        db_session,
        bob_role=WorkspaceRole.MEMBER,
        context_role=ContextRole.EDITOR,
        allowed_context=False,
    )
    worlds.append(world)
    await _assert_hidden(world, world.w, world.p, None)


async def test_key_scoped_to_another_workspace_gets_not_found(
    db_session: AsyncSession, worlds: list[World]
) -> None:
    world = await _build(db_session, bob_role=WorkspaceRole.MEMBER, context_role=ContextRole.EDITOR)
    worlds.append(world)
    set_mcp_key_workspace_scope(world.p)
    try:
        for tool in ("get_sleep_report", "rollback_sleep_run"):
            set_mcp_key_workspace_scope(None)
            expected = await _not_found_reply(world, tool)
            set_mcp_key_workspace_scope(world.p)
            assert await _call(world, tool, world.p) == expected, tool
    finally:
        set_mcp_key_workspace_scope(None)
    await _untouched(world)


async def test_agent_credential_without_a_binding_gets_not_found(
    db_session: AsyncSession, worlds: list[World]
) -> None:
    world = await _build(db_session, bob_role=WorkspaceRole.MEMBER, context_role=ContextRole.EDITOR)
    worlds.append(world)
    expected = {
        tool: await _not_found_reply(world, tool)
        for tool in ("get_sleep_report", "rollback_sleep_run")
    }
    set_agent_scope(AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=world.w))
    try:
        for tool, reply in expected.items():
            assert await _call(world, tool, world.w) == reply, tool
    finally:
        set_agent_scope(None)
    await _untouched(world)


async def test_report_without_a_context_gets_not_found(
    db_session: AsyncSession, worlds: list[World]
) -> None:
    world = await _build(db_session, bob_role=WorkspaceRole.ADMIN, with_context=False)
    worlds.append(world)
    await _assert_hidden(world, world.w, world.p, None)


@pytest.mark.parametrize(
    ("bob_role", "context_role"),
    [
        pytest.param(WorkspaceRole.MEMBER, ContextRole.EDITOR, id="context-editor"),
        pytest.param(WorkspaceRole.MEMBER, ContextRole.OWNER, id="context-owner"),
        pytest.param(WorkspaceRole.ADMIN, None, id="workspace-admin"),
    ],
)
async def test_editor_and_owner_still_read_and_roll_back(
    db_session: AsyncSession,
    worlds: list[World],
    bob_role: str,
    context_role: ContextRole | None,
) -> None:
    world = await _build(db_session, bob_role=bob_role, context_role=context_role)
    worlds.append(world)
    report = json.loads(await _call(world, "get_sleep_report", world.p))
    assert report["status"] == "success"
    assert report["action_count"] == 3
    # The session may point anywhere: the report's context decides.
    data = json.loads(await _call(world, "rollback_sleep_run", None))
    assert data["status"] == "rolled_back", data
    summary = data["rollback_summary"]
    assert (
        summary["edges_deleted"],
        summary["importance_restored"],
        summary["archives_restored"],
    ) == (1, 1, 1)
    world.db.expunge_all()
    rescored = await world.db.get(Memory, world.rescored_id)
    archived = await world.db.get(Memory, world.archived_id)
    assert rescored is not None and rescored.importance == pytest.approx(0.4)
    assert archived is not None and archived.deleted_at is None
    assert await world.db.get(NeuralMemoryEdge, world.edge_id) is None
