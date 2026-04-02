"""Integration tests for single collection 3-level isolation.

Tests the most critical security feature: ensuring workspace/context/user boundaries are enforced.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from db.qdrant import ensure_kagura_memories_collection
from models.auth import Context, Workspace, WorkspaceMember
from models.memory import Memory
from models.schemas import ExploreRequest, RecallRequest, RememberRequest
from services.memory_service import MemoryService


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
            role="owner" if uid == "owner1" else "member",
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
            role="owner" if uid == "owner2" else "member",
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


@pytest.mark.asyncio
class TestSingleCollectionIsolation:
    """Test 3-level isolation (workspace, context, user) in single collection design.

    Note: Tests that use remember→recall require DATABASE_URL to match the test DB,
    because process_pending_embedding creates its own DB session via get_db().
    """

    @pytest_asyncio.fixture(autouse=True)
    async def patch_create_task(self, monkeypatch):
        """Patch asyncio.create_task to track embedding tasks so tests can await them.

        In test environment, asyncio.create_task fires a background coroutine (embedding).
        Tests must await all pending embedding tasks before calling recall(), otherwise
        the Qdrant points won't exist yet and recall() returns empty results.

        This fixture replaces create_task with a version that collects tasks so that
        tests can call await asyncio.gather(*pending_tasks) after remember().
        """
        import asyncio

        original_create_task = asyncio.create_task
        pending_tasks: list = []

        def tracking_create_task(coro, **kwargs):
            task = original_create_task(coro, **kwargs)
            pending_tasks.append(task)
            return task

        monkeypatch.setattr(asyncio, "create_task", tracking_create_task)

        # Expose pending_tasks via a helper stored on the fixture return value
        self._pending_embedding_tasks = pending_tasks
        yield
        monkeypatch.setattr(asyncio, "create_task", original_create_task)

    async def test_cross_workspace_isolation(
        self, db_session, test_workspace1, test_workspace2, test_context1, test_context_ws2
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

        await asyncio.gather(*self._pending_embedding_tasks, return_exceptions=True)

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
        self, db_session, test_workspace1, test_context1, test_context2
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

        await asyncio.gather(*self._pending_embedding_tasks, return_exceptions=True)

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
        self, db_session, test_workspace1, test_context1
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

        await asyncio.gather(*self._pending_embedding_tasks, return_exceptions=True)

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
        self, db_session, test_workspace1, test_context1, test_context2
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

        await asyncio.gather(*self._pending_embedding_tasks, return_exceptions=True)

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
