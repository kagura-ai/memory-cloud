"""Integration tests for single collection 3-level isolation.

Tests the most critical security feature: ensuring workspace/context/user boundaries are enforced.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from auth.workspace_roles import WorkspaceRole
from db.qdrant import ensure_kagura_memories_collection
from models.auth import Context, Workspace, WorkspaceMember
from models.memory import Memory
from models.schemas import ExploreRequest, RecallRequest, RememberRequest
from services.memory_service import MemoryService


@pytest.fixture(autouse=True)
def patch_database_url(monkeypatch):
    """Ensure DATABASE_URL points to the test DB so background embedding tasks find rows.

    process_pending_embedding creates its own DB session via get_db(), which
    imports DATABASE_URL from config.database. That constant is set at module
    import time, so monkeypatching os.environ alone is not enough — we must
    also patch the module-level constant and reset the engine singleton so
    the background task creates a fresh engine pointing at the test DB.
    """
    import config.database
    import db.base
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setattr(config.database, "DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    # Reset engine singletons so get_db() recreates them with the patched URL
    db.base.engine = None
    db.base.async_session_factory = None
    db.base.sync_engine = None


@pytest_asyncio.fixture(autouse=True)
async def reset_redis_singleton():
    """Reset Redis singleton before each test to avoid event loop mismatch.

    The Redis async client binds to the event loop it was created in.
    Resetting the singleton ensures it is recreated fresh in each test's loop.
    """
    import db.redis as redis_module

    redis_module._redis_client = None
    yield
    # Close and reset after test to clean up
    try:
        await redis_module.close_redis()
    except Exception as exc:
        print(f"Warning: Redis cleanup failed: {exc}")
    redis_module._redis_client = None


@pytest_asyncio.fixture
async def test_workspace1(db_session):
    """Create test workspace 1 with user1 and user2 as members."""
    workspace = Workspace(
        id=uuid4(),
        name="Test Workspace 1",
        owner_user_id="owner1",
    )
    db_session.add(workspace)
    await db_session.flush()

    # Add user1 and user2 as members so get_context() access check passes
    for uid in ("owner1", "user1", "user2"):
        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=uid,
            role=WorkspaceRole.OWNER if uid == "owner1" else WorkspaceRole.MEMBER,
        )
        db_session.add(member)

    await db_session.commit()
    return workspace


@pytest_asyncio.fixture
async def test_workspace2(db_session):
    """Create test workspace 2 with user2 as member."""
    workspace = Workspace(
        id=uuid4(),
        name="Test Workspace 2",
        owner_user_id="owner2",
    )
    db_session.add(workspace)
    await db_session.flush()

    # Add user2 as member so get_context() access check passes
    for uid in ("owner2", "user2"):
        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=uid,
            role=WorkspaceRole.OWNER if uid == "owner2" else WorkspaceRole.MEMBER,
        )
        db_session.add(member)

    await db_session.commit()
    return workspace


@pytest_asyncio.fixture
async def test_context1(db_session, test_workspace1):
    """Create test context 1 in workspace1 (shared so all members can access)."""
    context = Context(
        id=uuid4(),
        workspace_id=test_workspace1.id,
        name="context1",
        display_name="Test Context 1",
        created_by="user1",
        is_private=False,
    )
    db_session.add(context)
    await db_session.commit()
    return context


@pytest_asyncio.fixture
async def test_context2(db_session, test_workspace1):
    """Create test context 2 in workspace1 (shared so all members can access)."""
    context = Context(
        id=uuid4(),
        workspace_id=test_workspace1.id,
        name="context2",
        display_name="Test Context 2",
        created_by="user1",
        is_private=False,
    )
    db_session.add(context)
    await db_session.commit()
    return context


@pytest_asyncio.fixture
async def test_context_ws2(db_session, test_workspace2):
    """Create test context in workspace2 (for cross-workspace isolation tests)."""
    context = Context(
        id=uuid4(),
        workspace_id=test_workspace2.id,
        name="context-ws2",
        display_name="Test Context WS2",
        created_by="owner2",
        is_private=False,
    )
    db_session.add(context)
    await db_session.commit()
    return context


@pytest_asyncio.fixture(autouse=True)
async def pending_embedding_tasks(monkeypatch):
    """Patch asyncio.create_task to track embedding tasks so tests can await them.

    Tests that fire embedding-triggering calls (e.g., service.remember()) must await
    all pending tasks before recall() — otherwise the Qdrant points are not yet
    written and recall() returns empty. Tests that need that wait request this
    fixture as a parameter and await it via asyncio.gather(*pending_embedding_tasks).
    """
    import asyncio

    original_create_task = asyncio.create_task
    pending_tasks: list = []

    def tracking_create_task(coro, **kwargs):
        task = original_create_task(coro, **kwargs)
        pending_tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", tracking_create_task)
    yield pending_tasks
    monkeypatch.setattr(asyncio, "create_task", original_create_task)


@pytest.mark.asyncio
class TestSingleCollectionIsolation:
    """Test 3-level isolation (workspace, context, user) in single collection design.

    Note: Tests that use remember→recall require DATABASE_URL to match the test DB,
    because process_pending_embedding creates its own DB session via get_db().
    """

    async def test_cross_workspace_isolation(
        self,
        db_session,
        test_workspace1,
        test_workspace2,
        test_context1,
        test_context_ws2,
        pending_embedding_tasks,
    ):
        """Test that workspace1 cannot see workspace2's memories.

        CRITICAL: This tests the primary security boundary.
        """
        # Setup: Create kagura_memories collection
        await ensure_kagura_memories_collection()

        service = MemoryService(db_session)

        # Workspace1 creates a memory in context1
        request1 = RememberRequest(
            summary="Workspace1 secret data",
            content="Confidential information for workspace1",
            type="note",
        )
        result1 = await service.remember(
            request1,
            user_id="user1",
            client="test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Workspace2 creates a memory in context_ws2
        request2 = RememberRequest(
            summary="Workspace2 secret data",
            content="Confidential information for workspace2",
            type="note",
        )
        result2 = await service.remember(
            request2,
            user_id="user2",
            client="test",
            current_context_id=test_context_ws2.id,
            current_workspace_id=test_workspace2.id,
        )

        # Wait for all background embedding tasks to complete before recall
        import asyncio

        await asyncio.gather(*pending_embedding_tasks, return_exceptions=True)

        # Test: Workspace1 recalls - should NOT see workspace2's memory
        recall_request = RecallRequest(query="secret data", k=10)
        recall_result = await service.recall(
            recall_request,
            user_id="user1",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Assertions
        memory_ids = [str(r.memory_id) for r in recall_result.results]
        assert str(result1.memory_id) in memory_ids, "Workspace1 should see its own memory"
        assert str(result2.memory_id) not in memory_ids, (
            "SECURITY FAIL: Workspace1 saw workspace2's memory!"
        )

        # Verify via direct query
        workspace1_memories = await db_session.execute(
            select(Memory).where(Memory.workspace_id == test_workspace1.id)
        )
        workspace1_count = len(list(workspace1_memories.scalars().all()))
        assert workspace1_count == 1, (
            f"Workspace1 should have exactly 1 memory, got {workspace1_count}"
        )

    async def test_cross_context_isolation_within_same_workspace(
        self,
        db_session,
        test_workspace1,
        test_context1,
        test_context2,
        pending_embedding_tasks,
    ):
        """Test that context1 cannot see context2's memories (same workspace).

        CRITICAL: This tests context-level isolation.
        """
        await ensure_kagura_memories_collection()

        service = MemoryService(db_session)

        # Create memory in context1
        request1 = RememberRequest(
            summary="Context1 data",
            content="Data in context1",
            type="note",
        )
        result1 = await service.remember(
            request1,
            user_id="user1",
            client="test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Create memory in context2 (same workspace, different context)
        request2 = RememberRequest(
            summary="Context2 data",
            content="Data in context2",
            type="note",
        )
        result2 = await service.remember(
            request2,
            user_id="user1",
            client="test",
            current_context_id=test_context2.id,
            current_workspace_id=test_workspace1.id,
        )

        # Wait for all background embedding tasks to complete before recall
        import asyncio

        await asyncio.gather(*pending_embedding_tasks, return_exceptions=True)

        # Test: Recall from context1 - should NOT see context2's memory
        recall_request = RecallRequest(query="data", k=10)
        recall_result = await service.recall(
            recall_request,
            user_id="user1",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Assertions
        memory_ids = [str(r.memory_id) for r in recall_result.results]
        assert str(result1.memory_id) in memory_ids, "Should see context1 memory"
        assert str(result2.memory_id) not in memory_ids, (
            "ISOLATION FAIL: Saw context2 memory from context1!"
        )

    async def test_shared_context_members_see_all_memories(
        self, db_session, test_workspace1, test_context1, pending_embedding_tasks
    ):
        """Test that in shared contexts, all members see all memories.

        Shared contexts are collaborative — no user-level isolation.
        """
        await ensure_kagura_memories_collection()

        service = MemoryService(db_session)

        # User1 creates a memory
        request1 = RememberRequest(
            summary="User1 shared note in context",
            content="User1's shared data",
            type="note",
        )
        result1 = await service.remember(
            request1,
            user_id="user1",
            client="test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # User2 creates a memory (same workspace, same shared context)
        request2 = RememberRequest(
            summary="User2 shared note in context",
            content="User2's shared data",
            type="note",
        )
        result2 = await service.remember(
            request2,
            user_id="user2",
            client="test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Wait for all background embedding tasks to complete before recall
        import asyncio

        await asyncio.gather(*pending_embedding_tasks, return_exceptions=True)

        # Test: User1 recalls in shared context — should see both memories
        recall_request = RecallRequest(query="shared note", k=10)
        recall_result = await service.recall(
            recall_request,
            user_id="user1",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # In shared contexts, all members' memories are visible
        memory_ids = [str(r.memory_id) for r in recall_result.results]
        assert str(result1.memory_id) in memory_ids, "User1 should see their own memory"
        assert str(result2.memory_id) in memory_ids, (
            "User1 should also see user2's memory (shared context)"
        )

    async def test_context_deletion_only_deletes_own_points(
        self,
        db_session,
        test_workspace1,
        test_context1,
        test_context2,
        pending_embedding_tasks,
    ):
        """Test that deleting context1 doesn't affect context2's points.

        CRITICAL: Prevents accidental data loss.
        """
        await ensure_kagura_memories_collection()

        service = MemoryService(db_session)

        # Create memories in both contexts
        request1 = RememberRequest(summary="Context1 memo", content="Data1", type="note")
        await service.remember(
            request1,
            "user1",
            "test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        request2 = RememberRequest(summary="Context2 memo", content="Data2", type="note")
        result2 = await service.remember(
            request2,
            "user1",
            "test",
            current_context_id=test_context2.id,
            current_workspace_id=test_workspace1.id,
        )

        # Wait for all background embedding tasks to complete before deletion/recall
        import asyncio

        await asyncio.gather(*pending_embedding_tasks, return_exceptions=True)

        # Delete context1's points
        from db.qdrant import delete_context_points

        await delete_context_points(str(test_workspace1.id), str(test_context1.id))

        # Test: Context2's memory should still exist
        recall_request = RecallRequest(query="memo", k=10)
        recall_result = await service.recall(
            recall_request,
            user_id="user1",
            current_context_id=test_context2.id,
            current_workspace_id=test_workspace1.id,
        )

        # Assertions
        memory_ids = [str(r.memory_id) for r in recall_result.results]
        assert str(result2.memory_id) in memory_ids, (
            "Context2 memory should survive context1 deletion"
        )
        assert len(memory_ids) == 1, f"Should have exactly 1 memory, got {len(memory_ids)}"

    async def test_graph_traversal_respects_context_boundary(
        self, db_session, test_workspace1, test_context1, test_context2
    ):
        """Test that graph explore() doesn't cross context boundaries.

        CRITICAL: Graph traversal security.
        """
        await ensure_kagura_memories_collection()

        service = MemoryService(db_session)

        # Context1: Create 2 related memories
        req1 = RememberRequest(summary="Graph node A in context1", content="Content A", type="note")
        mem1 = await service.remember(
            req1,
            "user1",
            "test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        req2 = RememberRequest(summary="Graph node B in context1", content="Content B", type="note")
        mem2 = await service.remember(
            req2,
            "user1",
            "test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Context2: Create a memory
        req3 = RememberRequest(summary="Graph node C in context2", content="Content C", type="note")
        mem3 = await service.remember(
            req3,
            "user1",
            "test",
            current_context_id=test_context2.id,
            current_workspace_id=test_workspace1.id,
        )

        # Create edge in context1: A -> B
        from services.graph_service import GraphService

        graph_service = GraphService(
            user_id="user1",
            db=db_session,
            workspace_id=str(test_workspace1.id),
            context_id=str(test_context1.id),
        )
        await graph_service.add_edge(
            src_id=str(mem1.memory_id),
            dst_id=str(mem2.memory_id),
            rel_type="related_to",
            weight=0.8,
        )

        # Test: Explore from A - should only find B (not C from context2)
        explore_request = ExploreRequest(memory_id=mem1.memory_id, depth=2)
        explore_result = await service.explore(
            explore_request,
            user_id="user1",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Assertions
        related_ids = [str(r.memory_id) for r in explore_result.related_memories]
        assert str(mem2.memory_id) in related_ids, "Should find B (connected in same context)"
        assert str(mem3.memory_id) not in related_ids, (
            "ISOLATION FAIL: Found C from different context!"
        )

    async def test_required_parameters_validation(self, db_session):
        """Test that missing workspace_id/context_id/user_id raises errors.

        CRITICAL: Prevents silent security bypass.
        """
        from db.qdrant import search_memories_qdrant

        # Test: Missing workspace_id
        with pytest.raises(ValueError, match="Isolation requires"):
            await search_memories_qdrant(
                user_id="user1",
                query_vector=[0.1] * 512,
                workspace_id="",  # Empty
                context_id="ctx1",
            )

        # Test: Missing context_id
        with pytest.raises(ValueError, match="Isolation requires"):
            await search_memories_qdrant(
                user_id="user1",
                query_vector=[0.1] * 512,
                workspace_id="workspace1",
                context_id="",  # Empty
            )

        # Test: Missing user_id
        with pytest.raises(ValueError, match="Isolation requires"):
            await search_memories_qdrant(
                user_id="",  # Empty
                query_vector=[0.1] * 512,
                workspace_id="workspace1",
                context_id="ctx1",
            )


async def _insert_memory(db_session, *, workspace_id, context_id, user_id="user1", summary):
    """Insert a Memory row directly via the ORM and return its UUID.

    Used by explore() tests that don't need Qdrant — explore() reads from PostgreSQL
    only (graph + Memory table), so skipping service.remember() avoids the OpenAI
    embedding round-trip while still satisfying explore()'s data requirements.

    Returns the UUID rather than the ORM object so the caller never accesses a
    SQLAlchemy attribute on an object whose state may later be expired by a
    commit inside explore() — that would trigger a synchronous lazy-load and
    fail with MissingGreenlet in an async context.
    """
    memory = Memory(
        user_id=user_id,
        summary=summary,
        content=summary,
        type="note",
        client="test",
        workspace_id=workspace_id,
        context_id=context_id,
    )
    db_session.add(memory)
    await db_session.flush()
    memory_id = memory.id
    return memory_id


@pytest.mark.asyncio
class TestExploreAccessStats:
    """Test that explore() bumps access_count / last_used_at / accessed_by_clients
    on returned memories, matching the behavior of recall() and reference()
    (Issue #644).

    Real-DB integration tests — assertions read back the Memory rows post-call.
    """

    async def test_explore_bumps_seed_and_related_on_normal_path(
        self, db_session, test_workspace1, test_context1
    ):
        """Path C (normal success): seed + each related memory get bumped once."""
        service = MemoryService(db_session)

        seed_id = await _insert_memory(
            db_session,
            workspace_id=test_workspace1.id,
            context_id=test_context1.id,
            summary="Seed node for access stat test",
        )
        related_id = await _insert_memory(
            db_session,
            workspace_id=test_workspace1.id,
            context_id=test_context1.id,
            summary="Related node for access stat test",
        )

        from services.graph_service import GraphService

        graph_service = GraphService(
            user_id="user1",
            db=db_session,
            workspace_id=str(test_workspace1.id),
            context_id=str(test_context1.id),
        )
        await graph_service.add_edge(
            src_id=str(seed_id),
            dst_id=str(related_id),
            rel_type="related_to",
            weight=0.8,
        )
        await db_session.commit()

        # Baseline: fresh memories have access_count=0, last_used_at IS NULL.
        seed_row_before = (
            await db_session.execute(select(Memory).where(Memory.id == seed_id))
        ).scalar_one()
        assert seed_row_before.access_count == 0
        assert seed_row_before.last_used_at is None

        explore_result = await service.explore(
            ExploreRequest(memory_id=seed_id, depth=2),
            user_id="user1",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Sanity check: this exercises Path C (related memories present).
        related_ids = [str(r.memory_id) for r in explore_result.related_memories]
        assert str(related_id) in related_ids

        # Re-fetch from DB and assert both rows were bumped.
        db_session.expire_all()
        seed_row = (
            await db_session.execute(select(Memory).where(Memory.id == seed_id))
        ).scalar_one()
        related_row = (
            await db_session.execute(select(Memory).where(Memory.id == related_id))
        ).scalar_one()

        assert seed_row.access_count == 1
        assert seed_row.last_used_at is not None
        assert seed_row.accessed_by_clients == ["api"]

        assert related_row.access_count == 1
        assert related_row.last_used_at is not None
        assert related_row.accessed_by_clients == ["api"]

    async def test_explore_bumps_seed_only_when_seed_not_in_graph(
        self, db_session, test_workspace1, test_context1
    ):
        """Path A (seed_not_in_graph): only seed gets bumped, no related memories.

        Skips service.remember() entirely — explore() reads from PostgreSQL only
        and has_node() returns False for a memory with no graph entry, so direct
        Memory insertion is sufficient.
        """
        service = MemoryService(db_session)

        seed_id = await _insert_memory(
            db_session,
            workspace_id=test_workspace1.id,
            context_id=test_context1.id,
            summary="Lonely seed not in graph",
        )
        await db_session.commit()

        explore_result = await service.explore(
            ExploreRequest(memory_id=seed_id, depth=2),
            user_id="user1",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Sanity check: this exercises Path A.
        assert explore_result.metadata.get("reason") == "seed_not_in_graph"
        assert explore_result.related_memories == []

        db_session.expire_all()
        seed_row = (
            await db_session.execute(select(Memory).where(Memory.id == seed_id))
        ).scalar_one()
        assert seed_row.access_count == 1
        assert seed_row.last_used_at is not None
        assert seed_row.accessed_by_clients == ["api"]

    async def test_explore_bumps_seed_only_when_traversal_yields_empty(
        self, db_session, test_workspace1, test_context1
    ):
        """Path B (empty_traversal): seed in graph, but min_weight filters out everything.

        Skips service.remember() — direct Memory insert plus add_edge gives both
        the seed graph node (so has_node() returns True) and a related node that
        will be filtered out by min_weight.
        """
        service = MemoryService(db_session)

        seed_id = await _insert_memory(
            db_session,
            workspace_id=test_workspace1.id,
            context_id=test_context1.id,
            summary="Seed B for empty traversal path",
        )
        related_id = await _insert_memory(
            db_session,
            workspace_id=test_workspace1.id,
            context_id=test_context1.id,
            summary="Related B for empty traversal path",
        )

        from services.graph_service import GraphService

        graph_service = GraphService(
            user_id="user1",
            db=db_session,
            workspace_id=str(test_workspace1.id),
            context_id=str(test_context1.id),
        )
        # add_edge creates both nodes implicitly, so the seed IS in the graph
        # (has_node returns True). min_weight=2.0 then filters everything out.
        await graph_service.add_edge(
            src_id=str(seed_id),
            dst_id=str(related_id),
            rel_type="related_to",
            weight=0.05,
        )
        await db_session.commit()

        explore_result = await service.explore(
            ExploreRequest(memory_id=seed_id, depth=2, min_weight=2.0),
            user_id="user1",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Sanity check: this exercises Path B (related filtered out, but seed was in graph).
        assert explore_result.related_memories == []
        assert "suggestion" in explore_result.metadata

        db_session.expire_all()
        seed_row = (
            await db_session.execute(select(Memory).where(Memory.id == seed_id))
        ).scalar_one()
        related_row = (
            await db_session.execute(select(Memory).where(Memory.id == related_id))
        ).scalar_one()

        assert seed_row.access_count == 1
        assert seed_row.last_used_at is not None
        assert seed_row.accessed_by_clients == ["api"]
        # Related was NOT in the returned response → must NOT be bumped.
        assert related_row.access_count == 0
        assert related_row.last_used_at is None

    async def test_explore_repeated_calls_increment_count(
        self, db_session, test_workspace1, test_context1
    ):
        """Two explore() calls on the same seed (with an edge → Path C) bump both seed and
        related to access_count=2 and keep accessed_by_clients deduped to ["api"]."""
        service = MemoryService(db_session)

        seed_id = await _insert_memory(
            db_session,
            workspace_id=test_workspace1.id,
            context_id=test_context1.id,
            summary="Repeated seed for dedupe test",
        )
        related_id = await _insert_memory(
            db_session,
            workspace_id=test_workspace1.id,
            context_id=test_context1.id,
            summary="Related node for repeated dedupe test",
        )

        from services.graph_service import GraphService

        graph_service = GraphService(
            user_id="user1",
            db=db_session,
            workspace_id=str(test_workspace1.id),
            context_id=str(test_context1.id),
        )
        await graph_service.add_edge(
            src_id=str(seed_id),
            dst_id=str(related_id),
            rel_type="related_to",
            weight=0.8,
        )
        await db_session.commit()

        for _ in range(2):
            await service.explore(
                ExploreRequest(memory_id=seed_id, depth=2),
                user_id="user1",
                current_context_id=test_context1.id,
                current_workspace_id=test_workspace1.id,
            )

        db_session.expire_all()
        seed_row = (
            await db_session.execute(select(Memory).where(Memory.id == seed_id))
        ).scalar_one()
        related_row = (
            await db_session.execute(select(Memory).where(Memory.id == related_id))
        ).scalar_one()

        assert seed_row.access_count == 2
        assert seed_row.accessed_by_clients == ["api"]
        # Related is also bumped via Path C and dedupes the client too.
        assert related_row.access_count == 2
        assert related_row.accessed_by_clients == ["api"]
