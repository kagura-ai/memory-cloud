"""Tests for GraphRepository."""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

import pytest

from models.memory import GraphMemory
from repositories.graph import GraphRepository

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _scan(
    repository: GraphRepository,
    page_size: int,
    *,
    start: int = 0,
    until: Callable[[list[GraphMemory]], bool] = lambda _rows: False,
) -> list[GraphMemory]:
    """Read graphs through list() one skip/limit page at a time, from ``start``.

    Stops at the first empty page, or as soon as ``until`` holds for what was
    read, so a test does not read more of the shared table than it needs.
    """
    rows: list[GraphMemory] = []
    while not until(rows) and (
        page := await repository.list(skip=start + len(rows), limit=page_size)
    ):
        rows.extend(page)
    return rows


def _has_ids(wanted: set[int]) -> Callable[[list[GraphMemory]], bool]:
    """``until`` predicate: every id in ``wanted`` has been read."""
    return lambda rows: wanted <= {g.id for g in rows}


class TestGraphRepository:
    """Test GraphRepository for PostgreSQL graph storage."""

    @pytest.fixture
    async def repository(self, db_session):
        """Create GraphRepository with test DB session."""
        return GraphRepository(db_session)

    @pytest.fixture
    async def sample_graph(self, db_session):
        """Create sample graph in DB with unique user_id to avoid collisions."""
        uid = str(uuid4())
        graph = GraphMemory(
            user_id=f"test_user_{uid}",
            graph_data={"nodes": [], "links": []},
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
        result = await repository.get_by_user_id(sample_graph.user_id)

        assert result is not None
        assert result.user_id == sample_graph.user_id
        assert result.id == sample_graph.id

    @pytest.mark.asyncio
    async def test_get_by_user_id_nonexistent(self, repository):
        """Test getting graph for nonexistent user."""
        result = await repository.get_by_user_id("nonexistent_user")

        assert result is None

    @pytest.mark.asyncio
    async def test_list(self, repository, sample_graph):
        """Test listing graphs.

        The shared test database accumulates graphs committed by other tests,
        and list() returns the lowest ids first, so page until the new graph
        (the highest id) shows up instead of reading one default page.
        """
        results = await _scan(repository, page_size=100, until=_has_ids({sample_graph.id}))

        assert any(g.id == sample_graph.id for g in results)

    @pytest.mark.asyncio
    async def test_list_orders_by_id_not_physical_or_user_id_order(self, repository, db_session):
        """list() returns graphs in primary-key order (#2385).

        The user_ids sort opposite to the ids, and the first row is updated so
        Postgres writes its new version after the others: physical order is
        then (second, third, first), user_id order is (third, second, first),
        and only an ORDER BY id gives (first, second, third).
        """
        uid = uuid4().hex
        rows = [
            GraphMemory(user_id=f"order_{uid}_{label}", graph_data={"nodes": [], "links": []})
            for label in ("c", "b", "a")
        ]
        for row in rows:
            db_session.add(row)
            await db_session.flush()  # one INSERT per row: ids ascend in list order
        first, second, third = rows
        assert first.id < second.id < third.id

        first.graph_data = {"nodes": [{"id": "moved"}], "links": []}
        await db_session.flush()

        mine_ids = {first.id, second.id, third.id}
        listed = await _scan(repository, page_size=1000, until=_has_ids(mine_ids))
        ids = [g.id for g in listed]
        assert ids == sorted(ids)
        assert [i for i in ids if i in mine_ids] == [first.id, second.id, third.id]

    @pytest.mark.asyncio
    async def test_list_pages_are_contiguous(self, repository, db_session):
        """skip/limit pages follow each other in id order without gaps or overlap.

        Only this test's rows are compared, so graphs other sessions commit
        meanwhile (always with higher ids) cannot make it fail.
        """
        uid = uuid4().hex
        rows = [
            GraphMemory(user_id=f"contig_{uid}_{i}", graph_data={"nodes": [], "links": []})
            for i in range(5)
        ]
        db_session.add_all(rows)
        await db_session.flush()
        mine = sorted(g.id for g in rows)

        before_mine = await _scan(repository, page_size=1000, until=_has_ids({mine[0]}))
        start = [g.id for g in before_mine].index(mine[0])  # offset of this test's lowest id

        paged = await _scan(repository, page_size=2, start=start, until=_has_ids(set(mine)))
        paged_ids = [g.id for g in paged]
        assert paged_ids == sorted(set(paged_ids))  # ascending, nothing read twice
        assert [i for i in paged_ids if i in set(mine)] == mine  # nothing skipped

        page = await repository.list(skip=start + 1, limit=2)
        assert [g.id for g in page] == paged_ids[1:3]

    @pytest.mark.asyncio
    async def test_list_with_pagination(self, repository, db_session):
        """Test listing with pagination."""
        # Create multiple graphs with unique user_ids
        uid = str(uuid4())
        for i in range(3):
            graph = GraphMemory(
                user_id=f"page_user_{uid}_{i}",
                graph_data={"nodes": [], "links": []},
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
        uid = str(uuid4())
        graph_data = {
            "nodes": [{"id": "node1", "type": "memory"}],
            "links": [],
        }

        new_graph = GraphMemory(
            user_id=f"new_user_{uid}",
            graph_data=graph_data,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        created = await repository.create(new_graph)

        assert created.id is not None
        assert created.user_id == new_graph.user_id
        assert created.graph_data["nodes"][0]["id"] == "node1"

        # Verify in DB
        fetched = await repository.get_by_user_id(new_graph.user_id)
        assert fetched is not None

    @pytest.mark.asyncio
    async def test_update(self, repository, sample_graph, db_session):
        """Test updating existing graph."""
        # Update graph
        sample_graph.graph_data = {
            "nodes": [{"id": "node1", "type": "memory"}],
            "links": [],
        }
        sample_graph.updated_at = datetime.utcnow()

        updated = await repository.update(sample_graph.id, sample_graph)

        assert updated.graph_data["nodes"][0]["id"] == "node1"

        # Verify in DB
        fetched = await repository.get(sample_graph.id)
        assert fetched.graph_data["nodes"][0]["id"] == "node1"

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
        uid = str(uuid4())
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
            user_id=f"json_user_{uid}",
            graph_data=complex_graph,
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
            user_id=sample_graph.user_id,  # Same user as sample_graph
            graph_data={"nodes": [], "links": []},
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        # Should raise integrity error due to unique constraint
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db_session.add(duplicate_graph)
            await db_session.commit()
