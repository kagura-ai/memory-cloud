"""Integration tests for single collection 3-level isolation.

Tests the most critical security feature: ensuring workspace/context/user boundaries are enforced.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from db.qdrant import ensure_kagura_memories_collection
from models.auth import Context, Workspace
from models.memory import Memory
from models.schemas import ExploreRequest, RecallRequest, RememberRequest
from services.memory_service import MemoryService


@pytest.fixture
async def test_workspace1(db_session):
    """Create test workspace 1."""
    workspace = Workspace(
        id=uuid4(),
        name="Test Workspace 1",
        owner_user_id="owner1",
    )
    db_session.add(workspace)
    await db_session.commit()
    return workspace


@pytest.fixture
async def test_workspace2(db_session):
    """Create test workspace 2."""
    workspace = Workspace(
        id=uuid4(),
        name="Test Workspace 2",
        owner_user_id="owner2",
    )
    db_session.add(workspace)
    await db_session.commit()
    return workspace


@pytest.fixture
async def test_context1(db_session, test_workspace1):
    """Create test context 1 in workspace1."""
    context = Context(
        id=uuid4(),
        workspace_id=test_workspace1.id,
        name="context1",
        display_name="Test Context 1",
        created_by="user1",
    )
    db_session.add(context)
    await db_session.commit()
    return context


@pytest.fixture
async def test_context2(db_session, test_workspace1):
    """Create test context 2 in workspace1 (same workspace, different context)."""
    context = Context(
        id=uuid4(),
        workspace_id=test_workspace1.id,
        name="context2",
        display_name="Test Context 2",
        created_by="user1",
    )
    db_session.add(context)
    await db_session.commit()
    return context


@pytest.mark.asyncio
class TestSingleCollectionIsolation:
    """Test 3-level isolation (workspace, context, user) in single collection design."""

    async def test_cross_workspace_isolation(
        self, db_session, test_workspace1, test_workspace2, test_context1, test_context2
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

        # Workspace2 creates a memory in context2
        request2 = RememberRequest(
            summary="Workspace2 secret data",
            content="Confidential information for workspace2",
            type="note",
        )
        result2 = await service.remember(
            request2,
            user_id="user2",
            client="test",
            current_context_id=test_context2.id,
            current_workspace_id=test_workspace2.id,
        )

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

    async def test_cross_user_isolation_within_same_context(
        self, db_session, test_workspace1, test_context1
    ):
        """Test that user1 cannot see user2's memories (same context).

        CRITICAL: This tests user-level isolation.
        """
        await ensure_kagura_memories_collection()

        service = MemoryService(db_session)

        # User1 creates a memory
        request1 = RememberRequest(
            summary="User1 private note",
            content="User1's private data",
            type="note",
        )
        result1 = await service.remember(
            request1,
            user_id="user1",
            client="test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # User2 creates a memory (same workspace, same context)
        request2 = RememberRequest(
            summary="User2 private note",
            content="User2's private data",
            type="note",
        )
        result2 = await service.remember(
            request2,
            user_id="user2",
            client="test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Test: User1 recalls - should NOT see user2's memory
        recall_request = RecallRequest(query="private note", k=10)
        recall_result = await service.recall(
            recall_request,
            user_id="user1",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Assertions
        memory_ids = [str(r.memory_id) for r in recall_result.results]
        assert str(result1.memory_id) in memory_ids, "User1 should see their own memory"
        assert str(result2.memory_id) not in memory_ids, "ISOLATION FAIL: User1 saw user2's memory!"

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
        req1 = RememberRequest(summary="Node A", content="Content A", type="note")
        mem1 = await service.remember(
            req1,
            "user1",
            "test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        req2 = RememberRequest(summary="Node B", content="Content B", type="note")
        mem2 = await service.remember(
            req2,
            "user1",
            "test",
            current_context_id=test_context1.id,
            current_workspace_id=test_workspace1.id,
        )

        # Context2: Create a memory
        req3 = RememberRequest(summary="Node C", content="Content C", type="note")
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
        with pytest.raises(ValueError, match="3-level isolation requires"):
            await search_memories_qdrant(
                user_id="user1",
                query_vector=[0.1] * 512,
                workspace_id="",  # Empty
                context_id="ctx1",
            )

        # Test: Missing context_id
        with pytest.raises(ValueError, match="3-level isolation requires"):
            await search_memories_qdrant(
                user_id="user1",
                query_vector=[0.1] * 512,
                workspace_id="workspace1",
                context_id="",  # Empty
            )

        # Test: Missing user_id
        with pytest.raises(ValueError, match="3-level isolation requires"):
            await search_memories_qdrant(
                user_id="",  # Empty
                query_vector=[0.1] * 512,
                workspace_id="workspace1",
                context_id="ctx1",
            )
