"""Tests for GraphRepository."""

from datetime import datetime

import pytest

from models.memory import GraphMemory
from repositories.graph import GraphRepository


class TestGraphRepository:
    """Test GraphRepository for PostgreSQL graph storage."""

    @pytest.fixture
    async def repository(self, db_session):
        """Create GraphRepository with test DB session."""
        return GraphRepository(db_session)

    @pytest.fixture
    async def sample_graph(self, db_session):
        """Create sample graph in DB."""
        graph = GraphMemory(
            user_id="test_user",
            graph_data={"nodes": [], "links": []},
            node_count=0,
            edge_count=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db_session.add(graph)
        await db_session.commit()
        await db_session.refresh(graph)
        return graph

    @pytest.mark.asyncio
    async def test_get_existing(self, repository, sample_graph):
        """Test getting existing graph by ID."""
        result = await repository.get(sample_graph.id)

        assert result is not None
        assert result.id == sample_graph.id
        assert result.user_id == sample_graph.user_id

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repository):
        """Test getting nonexistent graph."""
        result = await repository.get(99999)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_user_id(self, repository, sample_graph):
        """Test getting graph by user_id."""
        result = await repository.get_by_user_id("test_user")

        assert result is not None
        assert result.user_id == "test_user"
        assert result.id == sample_graph.id

    @pytest.mark.asyncio
    async def test_get_by_user_id_nonexistent(self, repository):
        """Test getting graph for nonexistent user."""
        result = await repository.get_by_user_id("nonexistent_user")

        assert result is None

    @pytest.mark.asyncio
    async def test_list(self, repository, sample_graph):
        """Test listing graphs."""
        results = await repository.list()

        assert len(results) > 0
        assert any(g.id == sample_graph.id for g in results)

    @pytest.mark.asyncio
    async def test_list_with_pagination(self, repository, db_session):
        """Test listing with pagination."""
        # Create multiple graphs
        for i in range(3):
            graph = GraphMemory(
                user_id=f"user_{i}",
                graph_data={"nodes": [], "links": []},
                node_count=0,
                edge_count=0,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db_session.add(graph)
        await db_session.commit()

        # Test pagination
        page1 = await repository.list(skip=0, limit=2)

        assert len(page1) <= 2

    @pytest.mark.asyncio
    async def test_create(self, repository, db_session):
        """Test creating new graph."""
        graph_data = {
            "nodes": [{"id": "node1", "type": "memory"}],
            "links": [],
        }

        new_graph = GraphMemory(
            user_id="new_user",
            graph_data=graph_data,
            node_count=1,
            edge_count=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        created = await repository.create(new_graph)

        assert created.id is not None
        assert created.user_id == "new_user"
        assert created.node_count == 1

        # Verify in DB
        fetched = await repository.get_by_user_id("new_user")
        assert fetched is not None

    @pytest.mark.asyncio
    async def test_update(self, repository, sample_graph, db_session):
        """Test updating existing graph."""
        # Update graph
        sample_graph.graph_data = {
            "nodes": [{"id": "node1", "type": "memory"}],
            "links": [],
        }
        sample_graph.node_count = 1
        sample_graph.updated_at = datetime.utcnow()

        updated = await repository.update(sample_graph)

        assert updated.node_count == 1

        # Verify in DB
        fetched = await repository.get(sample_graph.id)
        assert fetched.node_count == 1

    @pytest.mark.asyncio
    async def test_delete(self, repository, sample_graph):
        """Test deleting graph."""
        graph_id = sample_graph.id

        await repository.delete(graph_id)

        # Verify deleted
        result = await repository.get(graph_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_graph_data_json_storage(self, repository, db_session):
        """Test that graph_data is stored as JSON."""
        complex_graph = {
            "nodes": [
                {"id": "n1", "type": "memory", "data": {"text": "test"}},
                {"id": "n2", "type": "memory", "data": {"text": "test2"}},
            ],
            "links": [
                {"source": "n1", "target": "n2", "weight": 0.8},
            ],
        }

        graph = GraphMemory(
            user_id="json_user",
            graph_data=complex_graph,
            node_count=2,
            edge_count=1,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        created = await repository.create(graph)

        # Fetch and verify JSON structure
        fetched = await repository.get(created.id)
        assert fetched.graph_data["nodes"][0]["id"] == "n1"
        assert fetched.graph_data["links"][0]["weight"] == 0.8

    @pytest.mark.asyncio
    async def test_one_graph_per_user(self, repository, sample_graph, db_session):
        """Test that each user can have only one graph (unique constraint)."""
        # Try to create another graph for same user
        duplicate_graph = GraphMemory(
            user_id="test_user",  # Same user as sample_graph
            graph_data={"nodes": [], "links": []},
            node_count=0,
            edge_count=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        # Should raise integrity error due to unique constraint
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db_session.add(duplicate_graph)
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_stats_update(self, repository, sample_graph):
        """Test updating node/edge counts."""
        sample_graph.node_count = 10
        sample_graph.edge_count = 15

        updated = await repository.update(sample_graph)

        assert updated.node_count == 10
        assert updated.edge_count == 15
