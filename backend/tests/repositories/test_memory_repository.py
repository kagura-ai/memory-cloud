"""Tests for MemoryRepository."""

from datetime import datetime
from uuid import uuid4

import pytest

from models.memory import Memory
from repositories.memory import MemoryRepository


class TestMemoryRepository:
    """Test MemoryRepository for PostgreSQL operations."""

    @pytest.fixture
    async def repository(self, db_session):
        """Create MemoryRepository with test DB session."""
        return MemoryRepository(db_session)

    @pytest.fixture
    async def sample_memory(self, db_session):
        """Create sample memory in DB."""
        memory = Memory(
            id=uuid4(),
            user_id="test_user",
            summary="Test memory",
            content="Test content",
            type="code",
            scope="working",
            created_at=datetime.utcnow(),
        )
        db_session.add(memory)
        await db_session.commit()
        await db_session.refresh(memory)
        return memory

    @pytest.mark.asyncio
    async def test_get_existing(self, repository, sample_memory):
        """Test getting existing memory by ID."""
        result = await repository.get(sample_memory.id)

        assert result is not None
        assert result.id == sample_memory.id
        assert result.summary == sample_memory.summary
        assert result.user_id == sample_memory.user_id

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repository):
        """Test getting nonexistent memory."""
        nonexistent_id = uuid4()
        result = await repository.get(nonexistent_id)

        assert result is None

    @pytest.mark.asyncio
    async def test_list_all(self, repository, sample_memory):
        """Test listing all memories."""
        results = await repository.list()

        assert len(results) > 0
        assert any(m.id == sample_memory.id for m in results)

    @pytest.mark.asyncio
    async def test_list_with_pagination(self, repository, db_session):
        """Test listing with pagination."""
        # Create multiple memories
        for i in range(5):
            memory = Memory(
                id=uuid4(),
                user_id="test_user",
                summary=f"Memory {i}",
                content=f"Content {i}",
                type="code",
                scope="working",
                created_at=datetime.utcnow(),
            )
            db_session.add(memory)
        await db_session.commit()

        # Test pagination
        page1 = await repository.list(skip=0, limit=2)
        page2 = await repository.list(skip=2, limit=2)

        assert len(page1) == 2
        assert len(page2) >= 2
        # Pages should not overlap
        page1_ids = {m.id for m in page1}
        page2_ids = {m.id for m in page2}
        assert page1_ids.isdisjoint(page2_ids)

    @pytest.mark.asyncio
    async def test_list_with_user_filter(self, repository, db_session):
        """Test listing with user_id filter."""
        # Create memories for different users
        memory1 = Memory(
            id=uuid4(),
            user_id="user1",
            summary="User1 memory",
            content="Content",
            type="code",
            scope="working",
            created_at=datetime.utcnow(),
        )
        memory2 = Memory(
            id=uuid4(),
            user_id="user2",
            summary="User2 memory",
            content="Content",
            type="code",
            scope="working",
            created_at=datetime.utcnow(),
        )
        db_session.add_all([memory1, memory2])
        await db_session.commit()

        # Filter by user_id
        results = await repository.list(filters={"user_id": "user1"})

        assert all(m.user_id == "user1" for m in results)
        assert any(m.id == memory1.id for m in results)

    @pytest.mark.asyncio
    async def test_list_with_scope_filter(self, repository, db_session):
        """Test listing with scope filter."""
        # Create memories with different scopes
        working = Memory(
            id=uuid4(),
            user_id="test_user",
            summary="Working memory",
            content="Content",
            type="code",
            scope="working",
            created_at=datetime.utcnow(),
        )
        persistent = Memory(
            id=uuid4(),
            user_id="test_user",
            summary="Persistent memory",
            content="Content",
            type="code",
            scope="persistent",
            created_at=datetime.utcnow(),
        )
        db_session.add_all([working, persistent])
        await db_session.commit()

        # Filter by scope
        results = await repository.list(filters={"scope": "working"})

        assert all(m.scope == "working" for m in results)

    @pytest.mark.asyncio
    async def test_list_with_type_filter(self, repository, db_session):
        """Test listing with type filter."""
        # Create memories with different types
        code = Memory(
            id=uuid4(),
            user_id="test_user",
            summary="Code memory",
            content="Content",
            type="code",
            scope="working",
            created_at=datetime.utcnow(),
        )
        note = Memory(
            id=uuid4(),
            user_id="test_user",
            summary="Note memory",
            content="Content",
            type="note",
            scope="working",
            created_at=datetime.utcnow(),
        )
        db_session.add_all([code, note])
        await db_session.commit()

        # Filter by type
        results = await repository.list(filters={"type": "code"})

        assert all(m.type == "code" for m in results)

    @pytest.mark.asyncio
    async def test_list_sorted_by_created_at(self, repository, db_session):
        """Test that list is sorted by created_at DESC."""
        results = await repository.list(limit=10)

        if len(results) > 1:
            # Check descending order
            for i in range(len(results) - 1):
                assert results[i].created_at >= results[i + 1].created_at

    @pytest.mark.asyncio
    async def test_create(self, repository, db_session):
        """Test creating new memory."""
        new_memory = Memory(
            id=uuid4(),
            user_id="test_user",
            summary="New memory",
            content="New content",
            type="decision",
            scope="working",
            created_at=datetime.utcnow(),
        )

        created = await repository.create(new_memory)

        assert created.id == new_memory.id
        assert created.summary == new_memory.summary

        # Verify in DB
        fetched = await repository.get(created.id)
        assert fetched is not None
        assert fetched.summary == "New memory"

    @pytest.mark.asyncio
    async def test_update(self, repository, sample_memory, db_session):
        """Test updating existing memory."""
        # Update memory
        sample_memory.summary = "Updated summary"
        sample_memory.importance = 0.9

        updated = await repository.update(sample_memory)

        assert updated.summary == "Updated summary"
        assert updated.importance == 0.9

        # Verify in DB
        fetched = await repository.get(sample_memory.id)
        assert fetched.summary == "Updated summary"

    @pytest.mark.asyncio
    async def test_delete(self, repository, sample_memory):
        """Test deleting memory."""
        memory_id = sample_memory.id

        # Delete
        await repository.delete(memory_id)

        # Verify deleted
        result = await repository.get(memory_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_count(self, repository, db_session):
        """Test counting memories."""
        # Create known number of memories
        for i in range(3):
            memory = Memory(
                id=uuid4(),
                user_id="count_user",
                summary=f"Memory {i}",
                content="Content",
                type="code",
                scope="working",
                created_at=datetime.utcnow(),
            )
            db_session.add(memory)
        await db_session.commit()

        # Count for user
        count = await repository.count(filters={"user_id": "count_user"})

        assert count >= 3

    @pytest.mark.asyncio
    async def test_get_by_id_alias(self, repository, sample_memory):
        """Test get_by_id is alias for get."""
        result = await repository.get_by_id(sample_memory.id)

        assert result is not None
        assert result.id == sample_memory.id
