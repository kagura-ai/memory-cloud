"""Tests for MemoryService."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import (
    ForgetRequest,
    RecallRequest,
    RememberRequest,
)
from services.memory_service import MemoryService


class TestMemoryServiceInit:
    """Test MemoryService initialization."""

    def test_init(self):
        """Test MemoryService creates all sub-services."""
        mock_db = MagicMock()
        service = MemoryService(mock_db)
        assert service.db == mock_db
        assert service.memory_repo is not None
        assert service.embedding_service is not None
        assert service.search_service is not None


class TestRecall:
    """Test recall (search) operations."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_recall_requires_context(self, service):
        """recall() requires current_workspace_id and current_context_id."""
        request = RecallRequest(query="test query", k=5)

        with pytest.raises(ValueError, match="requires current_workspace_id"):
            await service.recall(request=request, user_id="test_user")

    @pytest.mark.asyncio
    async def test_recall_basic(self, service, context_id, workspace_id):
        """Test basic recall with mocked search."""
        request = RecallRequest(query="test query", k=5)

        memory_id = str(uuid4())

        # Mock search results
        search_results = [
            {
                "id": memory_id,
                "score": 0.9,
                "hybrid_score": 0.9,
                "payload": {
                    "summary": "Test result",
                    "type": "code",
                    "created_at": datetime.utcnow().isoformat(),
                },
            }
        ]
        service.search_service.hybrid_search = AsyncMock(return_value=search_results)

        # Mock DB execute for PostgreSQL memory fetch
        mock_memory = MagicMock()
        mock_memory.id = memory_id
        mock_memory.summary = "Test result"
        mock_memory.context_summary = None
        mock_memory.type = "code"
        mock_memory.importance = 0.8
        mock_memory.scope = "working"
        mock_memory.created_at = datetime.utcnow()
        mock_memory.client = "test"
        mock_memory.tags = []
        mock_memory.context = None
        mock_memory.source_uri = None
        mock_memory.source_type = None

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_memory]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        service.db.execute = AsyncMock(return_value=mock_result)
        service.db.commit = AsyncMock()

        service.memory_repo.update_access_stats = AsyncMock()
        service._check_and_promote = AsyncMock()

        response = await service.recall(
            request=request,
            user_id="test_user",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
        )

        assert response.results is not None
        assert len(response.results) > 0

    @pytest.mark.asyncio
    async def test_recall_no_results(self, service, context_id, workspace_id):
        """Test recall with no results."""
        request = RecallRequest(query="nonexistent", k=5)

        mock_context = MagicMock(
            id=context_id, workspace_id=workspace_id, is_private=True, created_by="test_user"
        )
        service.context_service.get_context = AsyncMock(return_value=mock_context)

        service.search_service.hybrid_search = AsyncMock(return_value=[])

        response = await service.recall(
            request=request,
            user_id="test_user",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
        )

        assert len(response.results) == 0


class TestRemember:
    """Test remember (store) operations."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_remember_requires_context(self, service):
        """remember() requires current_context_id."""
        request = RememberRequest(
            summary="Test memory for search",
            content="Test content body",
            type="code",
        )

        with pytest.raises(ValueError, match="requires current_context_id"):
            await service.remember(
                request=request,
                user_id="test_user",
            )


class TestReference:
    """Test reference (get details) operations."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_reference_not_found(self, service):
        """reference() with nonexistent memory raises NotFoundException."""
        from utils.exceptions import NotFoundException

        memory_id = uuid4()
        service.memory_repo.get = AsyncMock(return_value=None)

        with pytest.raises(NotFoundException):
            await service.reference(memory_id=memory_id, user_id="test_user")

    @pytest.mark.asyncio
    async def test_reference_found(self, service):
        """reference() returns full memory details."""
        memory_id = uuid4()
        mock_memory = MagicMock(
            id=memory_id,
            user_id="test_user",
            summary="Test",
            content="Test content",
            context_summary="Context",
            details={"key": "value"},
            type="code",
            importance=0.8,
            tags=["python"],
            context={"description": "test context"},
            scope="working",
            client="claude",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            embedding_status="success",
            workspace_id=uuid4(),
            context_id=uuid4(),
            deleted_at=None,
            source_uri=None,
            source_type=None,
        )
        service.memory_repo.get = AsyncMock(return_value=mock_memory)
        service.memory_repo.update_access_stats = AsyncMock()
        service.db.commit = AsyncMock()

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch("repositories.neural_edge.NeuralEdgeRepository") as mock_edge_cls,
        ):
            mock_perm = MagicMock()
            mock_perm.can_access_memory = AsyncMock(return_value=True)
            mock_perm_cls.return_value = mock_perm

            # Issue #440: reference() now fetches declared_link refs after the
            # access check. With both edge lists empty, _fetch_declared_link_refs
            # short-circuits before touching db.execute.
            mock_edge_repo = MagicMock()
            mock_edge_repo.get_outgoing_edges = AsyncMock(return_value=[])
            mock_edge_repo.get_incoming_edges = AsyncMock(return_value=[])
            mock_edge_cls.return_value = mock_edge_repo

            response = await service.reference(memory_id=memory_id, user_id="test_user")

        assert response.memory_id == memory_id
        assert response.summary == "Test"
        assert response.scope == "working"
        assert response.outgoing_links == []
        assert response.outgoing_has_more is False
        assert response.incoming_links == []
        assert response.incoming_has_more is False


class TestReferenceWithLinks:
    """Issue #440: reference() exposes outgoing/incoming declared_link refs."""

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.fixture
    def source_memory(self, workspace_id, context_id):
        return MagicMock(
            id=uuid4(),
            user_id="user-a",
            summary="Source",
            content="src content",
            context_summary=None,
            details=None,
            type="note",
            importance=0.5,
            tags=[],
            context=None,
            scope="working",
            client="api",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            workspace_id=workspace_id,
            context_id=context_id,
            deleted_at=None,
            source_uri=None,
            source_type=None,
        )

    @staticmethod
    def _linked_memory(workspace_id, context_id, summary="linked", deleted=False):
        return MagicMock(
            id=uuid4(),
            user_id="user-a",
            summary=summary,
            type="note",
            importance=0.6,
            workspace_id=workspace_id,
            context_id=context_id,
            deleted_at=datetime.utcnow() if deleted else None,
        )

    @staticmethod
    def _edge(src_id, dst_id, weight=1.0):
        return MagicMock(
            src_id=src_id,
            dst_id=dst_id,
            weight=weight,
            edge_type="declared_link",
            created_at=datetime.utcnow(),
        )

    def _make_service(self, source_memory):
        service = MemoryService(MagicMock())
        service.memory_repo.get = AsyncMock(return_value=source_memory)
        service.memory_repo.update_access_stats = AsyncMock()
        service.db.commit = AsyncMock()
        return service

    @staticmethod
    def _bulk_select_returning(linked_memories):
        """Build the AsyncMock used as ``service.db.execute`` for the
        bulk Memory re-scope query in ``_fetch_declared_link_refs``."""
        execute_result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = linked_memories
        execute_result.scalars.return_value = scalars
        return AsyncMock(return_value=execute_result)

    @pytest.mark.asyncio
    async def test_returns_outgoing_declared_links(self, source_memory, workspace_id, context_id):
        service = self._make_service(source_memory)
        target_a = self._linked_memory(workspace_id, context_id, summary="A")
        target_b = self._linked_memory(workspace_id, context_id, summary="B")
        out_edges = [
            self._edge(source_memory.id, target_a.id, weight=0.9),
            self._edge(source_memory.id, target_b.id, weight=0.4),
        ]
        service.db.execute = self._bulk_select_returning([target_a, target_b])

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch("repositories.neural_edge.NeuralEdgeRepository") as mock_edge_cls,
        ):
            mock_perm = MagicMock()
            mock_perm.can_access_memory = AsyncMock(return_value=True)
            mock_perm_cls.return_value = mock_perm

            mock_edge_repo = MagicMock()
            mock_edge_repo.get_outgoing_edges = AsyncMock(return_value=out_edges)
            mock_edge_repo.get_incoming_edges = AsyncMock(return_value=[])
            mock_edge_cls.return_value = mock_edge_repo

            response = await service.reference(memory_id=source_memory.id, user_id="user-a")

        assert len(response.outgoing_links) == 2
        summaries = {ref.summary for ref in response.outgoing_links}
        assert summaries == {"A", "B"}
        assert response.outgoing_has_more is False
        assert response.incoming_links == []
        assert response.incoming_has_more is False

    @pytest.mark.asyncio
    async def test_returns_incoming_declared_links(self, source_memory, workspace_id, context_id):
        service = self._make_service(source_memory)
        backlink = self._linked_memory(workspace_id, context_id, summary="Origin")
        in_edges = [self._edge(backlink.id, source_memory.id)]
        service.db.execute = self._bulk_select_returning([backlink])

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch("repositories.neural_edge.NeuralEdgeRepository") as mock_edge_cls,
        ):
            mock_perm = MagicMock()
            mock_perm.can_access_memory = AsyncMock(return_value=True)
            mock_perm_cls.return_value = mock_perm

            mock_edge_repo = MagicMock()
            mock_edge_repo.get_outgoing_edges = AsyncMock(return_value=[])
            mock_edge_repo.get_incoming_edges = AsyncMock(return_value=in_edges)
            mock_edge_cls.return_value = mock_edge_repo

            response = await service.reference(memory_id=source_memory.id, user_id="user-a")

        assert response.outgoing_links == []
        assert len(response.incoming_links) == 1
        assert response.incoming_links[0].summary == "Origin"

    @pytest.mark.asyncio
    async def test_caps_at_50_and_sets_has_more(self, source_memory, workspace_id, context_id):
        """When >50 edges exist, response truncates to 50 and signals has_more."""
        service = self._make_service(source_memory)
        targets = [
            self._linked_memory(workspace_id, context_id, summary=f"T{i}") for i in range(51)
        ]
        # repository sees limit=51 and returns all 51; service should cap at 50.
        out_edges = [self._edge(source_memory.id, t.id) for t in targets]
        service.db.execute = self._bulk_select_returning(targets)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch("repositories.neural_edge.NeuralEdgeRepository") as mock_edge_cls,
        ):
            mock_perm = MagicMock()
            mock_perm.can_access_memory = AsyncMock(return_value=True)
            mock_perm_cls.return_value = mock_perm

            mock_edge_repo = MagicMock()
            mock_edge_repo.get_outgoing_edges = AsyncMock(return_value=out_edges)
            mock_edge_repo.get_incoming_edges = AsyncMock(return_value=[])
            mock_edge_cls.return_value = mock_edge_repo

            response = await service.reference(memory_id=source_memory.id, user_id="user-a")

            # Repo must have been called with limit=51 (cap+1) so service can
            # detect has_more without paginating.
            # Issue #741: declared-link filter pivoted from edge_types=[...] to
            # origin='declared'.
            from models.memory import EDGE_ORIGIN_DECLARED

            kwargs = mock_edge_repo.get_outgoing_edges.await_args.kwargs
            assert kwargs["limit"] == 51
            assert kwargs.get("origin") == EDGE_ORIGIN_DECLARED

        assert len(response.outgoing_links) == 50
        assert response.outgoing_has_more is True

    @pytest.mark.asyncio
    async def test_drops_soft_deleted_target(self, source_memory, workspace_id, context_id):
        """Soft-deleted linked memory is dropped silently (defense-in-depth)."""
        service = self._make_service(source_memory)
        live = self._linked_memory(workspace_id, context_id, summary="Live")
        dead = self._linked_memory(workspace_id, context_id, summary="Dead", deleted=True)
        out_edges = [
            self._edge(source_memory.id, live.id),
            self._edge(source_memory.id, dead.id),
        ]
        # Bulk re-scope SQL filters `deleted_at IS NULL`, so only `live` is
        # returned by the query — the test simulates that behavior directly.
        service.db.execute = self._bulk_select_returning([live])

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch("repositories.neural_edge.NeuralEdgeRepository") as mock_edge_cls,
        ):
            mock_perm = MagicMock()
            mock_perm.can_access_memory = AsyncMock(return_value=True)
            mock_perm_cls.return_value = mock_perm

            mock_edge_repo = MagicMock()
            mock_edge_repo.get_outgoing_edges = AsyncMock(return_value=out_edges)
            mock_edge_repo.get_incoming_edges = AsyncMock(return_value=[])
            mock_edge_cls.return_value = mock_edge_repo

            response = await service.reference(memory_id=source_memory.id, user_id="user-a")

        assert len(response.outgoing_links) == 1
        assert response.outgoing_links[0].summary == "Live"

    @pytest.mark.asyncio
    async def test_passes_declared_link_filter_to_repo(
        self, source_memory, workspace_id, context_id
    ):
        """Edge fetch is restricted to user-asserted (origin='declared') edges.

        Issue #741: discriminator pivoted from edge_type='declared_link' to
        origin='declared'. The consumer in ``_fetch_declared_link_refs`` now
        passes ``origin=EDGE_ORIGIN_DECLARED`` instead of
        ``edge_types=['declared_link']``.
        """
        from models.memory import EDGE_ORIGIN_DECLARED

        service = self._make_service(source_memory)
        service.db.execute = self._bulk_select_returning([])

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch("repositories.neural_edge.NeuralEdgeRepository") as mock_edge_cls,
        ):
            mock_perm = MagicMock()
            mock_perm.can_access_memory = AsyncMock(return_value=True)
            mock_perm_cls.return_value = mock_perm

            mock_edge_repo = MagicMock()
            mock_edge_repo.get_outgoing_edges = AsyncMock(return_value=[])
            mock_edge_repo.get_incoming_edges = AsyncMock(return_value=[])
            mock_edge_cls.return_value = mock_edge_repo

            await service.reference(memory_id=source_memory.id, user_id="user-a")

        out_kwargs = mock_edge_repo.get_outgoing_edges.await_args.kwargs
        in_kwargs = mock_edge_repo.get_incoming_edges.await_args.kwargs
        assert out_kwargs.get("origin") == EDGE_ORIGIN_DECLARED
        assert in_kwargs.get("origin") == EDGE_ORIGIN_DECLARED
        # Both edge fetches are scoped to the source memory's workspace+context.
        assert out_kwargs["workspace_id"] == str(workspace_id)
        assert out_kwargs["context_id"] == str(context_id)
        assert in_kwargs["workspace_id"] == str(workspace_id)
        assert in_kwargs["context_id"] == str(context_id)


class TestRememberRequest:
    """Test RememberRequest schema validation for #213/#215 fields."""

    def test_source_uri_accepted(self):
        """source_uri and source_type are accepted as optional fields."""
        req = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
            source_uri="vault://my-vault/note.md",
            source_type="vault",
        )
        assert req.source_uri == "vault://my-vault/note.md"
        assert req.source_type == "vault"

    def test_source_fields_optional(self):
        """source_uri/source_type default to None."""
        req = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
        )
        assert req.source_uri is None
        assert req.source_type is None

    def test_invalid_source_type_rejected(self):
        """Invalid source_type is rejected by Literal validation."""
        with pytest.raises(ValueError):
            RememberRequest(
                summary="Test memory for search quality",
                content="Test content",
                type="note",
                source_type="invalid_type",
            )

    def test_linked_fields_accepted(self):
        """linked_memory_ids and linked_source_uris are accepted."""
        target_id = uuid4()
        req = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
            linked_memory_ids=[target_id],
            linked_source_uris=["vault://my-vault/other.md"],
        )
        assert req.linked_memory_ids == [target_id]
        assert req.linked_source_uris == ["vault://my-vault/other.md"]


class TestDeclaredLinks:
    """Test _create_declared_links logic (#215)."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_skips_when_no_links(self, service):
        """No-op when neither linked_memory_ids nor linked_source_uris provided."""
        request = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
        )
        # Should return immediately without touching DB
        await service._create_declared_links(
            memory_id=uuid4(),
            request=request,
            user_id="test_user",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
        )
        service.db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_without_isolation(self, service):
        """Skips when workspace_id or context_id is None."""
        request = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
            linked_memory_ids=[uuid4()],
        )
        await service._create_declared_links(
            memory_id=uuid4(),
            request=request,
            user_id="test_user",
            workspace_id=None,
            context_id=None,
        )
        service.db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_link_filtered(self, service):
        """Self-links are filtered out before DB query."""
        memory_id = uuid4()
        request = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
            linked_memory_ids=[memory_id],  # self-link
        )
        # Mock the validation query to return empty (self-link filtered before query)
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        service.db.execute = AsyncMock(return_value=mock_result)

        await service._create_declared_links(
            memory_id=memory_id,
            request=request,
            user_id="test_user",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
        )
        # The empty requested_ids list means no DB query at all
        service.db.execute.assert_not_called()


class TestTagCooccurrenceSeeding:
    """Unit tests for _create_tag_cooccurrence_seed_edges (Issue #223).

    Mock-based: the function lives at module scope in memory_service.py. We
    pass a fake AsyncSession + Memory and assert which paths short-circuit vs
    invoke create_edge_if_absent.
    """

    @pytest.fixture
    def base_memory(self):
        """A memory with two tags, in a fully-isolated (ws, ctx) scope."""
        from models.memory import Memory

        m = Memory(
            id=uuid4(),
            user_id="user-1",
            workspace_id=uuid4(),
            context_id=uuid4(),
            summary="seed summary",
            content="seed content",
            type="note",
            tags=["python", "backend", "memory-cloud"],
            scope="working",
            client="test",
        )
        return m

    @pytest.fixture
    def cfg(self):
        from neural.config import NeuralMemoryConfig

        return NeuralMemoryConfig()  # defaults: enabled=True, min_shared=2, etc.

    def _async_session_with(
        self,
        *,
        hub_tags: list[str] | None,
        candidates: list,
        table_exists: bool = True,
    ):
        """Build a MagicMock AsyncSession.

        Three execute() calls happen in the happy path, in order:
        1. ``SELECT to_regclass('hub_tag_cache')`` (pre-migration guard)
        2. ``SELECT hub_tags FROM hub_tag_cache WHERE ...`` (hub-tag fetch)
        3. ``SELECT id, ... FROM memories WHERE ...`` (candidate query)

        ``table_exists=False`` simulates the pre-migration window where the
        ``hub_tag_cache`` table does not yet exist; the function should
        return immediately after the first execute().
        """
        session = MagicMock()
        # to_regclass result: .scalar() returns 'hub_tag_cache' or None
        table_result = MagicMock()
        table_result.scalar = MagicMock(return_value="hub_tag_cache" if table_exists else None)
        # hub-tag row: scalar_one_or_none() returns the hub_tags list (or None).
        hub_result = MagicMock()
        hub_result.scalar_one_or_none = MagicMock(return_value=hub_tags)
        # candidate query: .all() returns the rows
        cand_result = MagicMock()
        cand_result.all = MagicMock(return_value=candidates)
        if table_exists:
            session.execute = AsyncMock(side_effect=[table_result, hub_result, cand_result])
        else:
            session.execute = AsyncMock(side_effect=[table_result])
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        # SAVEPOINT context manager
        savepoint = MagicMock()
        savepoint.__aenter__ = AsyncMock(return_value=None)
        savepoint.__aexit__ = AsyncMock(return_value=None)
        session.begin_nested = MagicMock(return_value=savepoint)
        return session

    @pytest.mark.asyncio
    async def test_disabled_config_is_noop(self, base_memory, cfg):
        from services import memory_service as ms

        cfg.tag_cooccurrence_enabled = False
        session = MagicMock()
        session.execute = AsyncMock()
        with patch(
            "neural.config.NeuralMemoryConfig.from_db",
            new=AsyncMock(return_value=cfg),
        ):
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_tags_is_noop(self, base_memory, cfg):
        from services import memory_service as ms

        base_memory.tags = []
        session = MagicMock()
        session.execute = AsyncMock()
        with patch(
            "neural.config.NeuralMemoryConfig.from_db",
            new=AsyncMock(return_value=cfg),
        ):
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_workspace_is_noop(self, base_memory, cfg):
        from services import memory_service as ms

        base_memory.workspace_id = None
        session = MagicMock()
        session.execute = AsyncMock()
        with patch(
            "neural.config.NeuralMemoryConfig.from_db",
            new=AsyncMock(return_value=cfg),
        ):
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_migration_skips_when_table_missing(self, base_memory, cfg):
        """Copilot loop 1: if hub_tag_cache table does not exist (pre-migration
        deploy window), the function returns silently after the to_regclass
        check — no idempotency call, no hub fetch, no candidate query."""
        from services import memory_service as ms

        edge_repo_cls = MagicMock()
        repo_inst = MagicMock()
        repo_inst.get_outgoing_edges = AsyncMock(return_value=[])
        edge_repo_cls.return_value = repo_inst

        session = self._async_session_with(hub_tags=None, candidates=[], table_exists=False)
        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=cfg),
            ),
            patch("repositories.neural_edge.NeuralEdgeRepository", new=edge_repo_cls),
        ):
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)

        # Only one execute: the to_regclass check. No edge_repo, no hub fetch,
        # no candidate query.
        assert session.execute.await_count == 1
        repo_inst.get_outgoing_edges.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotency_guard_skips_when_seeded(self, base_memory, cfg):
        """If tag_cooccurrence edges already exist for this memory, skip the
        SQL candidate query entirely. Issue #741: edge_type='tag_cooccurrence'
        merged into 'neural_association'; the discriminator moved to
        ``edge_metadata['source']`` on hebbian-origin rows. Pure-semantic
        edges (origin='semantic', no source metadata) must NOT trigger this skip."""
        from services import memory_service as ms

        # Build a fake hebbian edge that carries the tag_cooccurrence stamp.
        seeded_edge = MagicMock()
        seeded_edge.edge_metadata = {"source": "tag_cooccurrence"}

        edge_repo_cls = MagicMock()
        repo_inst = MagicMock()
        repo_inst.get_outgoing_edges = AsyncMock(return_value=[seeded_edge])
        edge_repo_cls.return_value = repo_inst

        # to_regclass returns table; idempotency then short-circuits
        session = self._async_session_with(hub_tags=None, candidates=[])
        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=cfg),
            ),
            patch("repositories.neural_edge.NeuralEdgeRepository", new=edge_repo_cls),
        ):
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)

        # to_regclass ran (1 execute), then idempotency fired
        assert session.execute.await_count == 1
        repo_inst.get_outgoing_edges.assert_awaited_once()
        # Post-#741: filter pivots from edge_types=[...] to origin='hebbian',
        # then Python-side scan of edge_metadata['source'].
        from models.memory import EDGE_ORIGIN_HEBBIAN

        call_kwargs = repo_inst.get_outgoing_edges.call_args.kwargs
        assert call_kwargs.get("origin") == EDGE_ORIGIN_HEBBIAN
        assert "edge_types" not in call_kwargs or call_kwargs["edge_types"] is None

    @pytest.mark.asyncio
    async def test_all_tags_are_hub_skips_query(self, base_memory, cfg):
        """When every tag on the new memory is in the hub-set for this
        (workspace, context), there are no non-hub tags left to correlate on
        and we short-circuit before issuing the candidate query."""
        from services import memory_service as ms

        edge_repo_cls = MagicMock()
        repo_inst = MagicMock()
        repo_inst.get_outgoing_edges = AsyncMock(return_value=[])
        edge_repo_cls.return_value = repo_inst

        # Every tag on the memory is in the hub set
        session = self._async_session_with(hub_tags=list(base_memory.tags), candidates=[])
        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=cfg),
            ),
            patch("repositories.neural_edge.NeuralEdgeRepository", new=edge_repo_cls),
        ):
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)

        # Two executes: to_regclass + hub-tag SELECT. Candidate query never fired.
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_creates_edges_with_correct_weight_and_confidence(self, base_memory, cfg):
        """Two candidates with shared=2 and shared=3 produce edges with the
        spec'd weight (0.25, 0.35) and confidence (0.50, 0.75)."""
        from services import memory_service as ms

        cand_a_id = uuid4()
        cand_b_id = uuid4()
        candidates = [
            MagicMock(id=cand_b_id, shared_count=3),  # higher shared first (ORDER BY DESC)
            MagicMock(id=cand_a_id, shared_count=2),
        ]
        session = self._async_session_with(hub_tags=None, candidates=candidates)

        repo_inst = MagicMock()
        repo_inst.get_outgoing_edges = AsyncMock(return_value=[])
        repo_inst.create_edge_if_absent = AsyncMock(side_effect=[MagicMock(), MagicMock()])

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=cfg),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                new=MagicMock(return_value=repo_inst),
            ),
        ):
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)

        assert repo_inst.create_edge_if_absent.await_count == 2
        # First call: shared=3 → weight=0.35, confidence=0.75
        # Issue #741: edge_type='tag_cooccurrence' merged into
        # 'neural_association'; the discriminator moved to
        # edge_metadata['source'].
        from models.memory import EDGE_TYPE_NEURAL_ASSOCIATION

        first = repo_inst.create_edge_if_absent.call_args_list[0].kwargs
        assert first["edge_type"] == EDGE_TYPE_NEURAL_ASSOCIATION
        assert first.get("edge_metadata") == {"source": "tag_cooccurrence"}
        assert first["dst_id"] == cand_b_id
        assert first["weight"] == pytest.approx(0.35)
        assert first["confidence"] == pytest.approx(0.75)
        # Second call: shared=2 → weight=0.25, confidence=0.50
        second = repo_inst.create_edge_if_absent.call_args_list[1].kwargs
        assert second["dst_id"] == cand_a_id
        assert second["weight"] == pytest.approx(0.25)
        assert second["confidence"] == pytest.approx(0.50)
        session.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_weight_capped_at_max(self, base_memory, cfg):
        """4+ shared tags caps weight at 0.40, confidence at 1.00."""
        from services import memory_service as ms

        candidates = [MagicMock(id=uuid4(), shared_count=7)]
        session = self._async_session_with(hub_tags=None, candidates=candidates)
        repo_inst = MagicMock()
        repo_inst.get_outgoing_edges = AsyncMock(return_value=[])
        repo_inst.create_edge_if_absent = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=cfg),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                new=MagicMock(return_value=repo_inst),
            ),
        ):
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)

        kw = repo_inst.create_edge_if_absent.call_args.kwargs
        assert kw["weight"] == pytest.approx(0.40)
        assert kw["confidence"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_degree_cap_truncates_writes(self, base_memory, cfg):
        """At ``tag_cooccurrence_max_degree_per_node`` writes, stop creating."""
        from services import memory_service as ms

        cfg.tag_cooccurrence_max_degree_per_node = 2  # tiny cap for the test
        candidates = [
            MagicMock(id=uuid4(), shared_count=3),
            MagicMock(id=uuid4(), shared_count=3),
            MagicMock(id=uuid4(), shared_count=3),  # would exceed cap
        ]
        session = self._async_session_with(hub_tags=None, candidates=candidates)
        repo_inst = MagicMock()
        repo_inst.get_outgoing_edges = AsyncMock(return_value=[])
        repo_inst.create_edge_if_absent = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=cfg),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                new=MagicMock(return_value=repo_inst),
            ),
        ):
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)

        # Only 2 writes despite 3 candidates
        assert repo_inst.create_edge_if_absent.await_count == 2

    @pytest.mark.asyncio
    async def test_per_edge_failure_does_not_abort_remaining(self, base_memory, cfg):
        """SAVEPOINT-per-edge: one failure logs a warning but the next edge
        still attempts."""
        from services import memory_service as ms

        candidates = [
            MagicMock(id=uuid4(), shared_count=2),
            MagicMock(id=uuid4(), shared_count=3),
        ]
        session = self._async_session_with(hub_tags=None, candidates=candidates)
        repo_inst = MagicMock()
        repo_inst.get_outgoing_edges = AsyncMock(return_value=[])
        # First call raises, second succeeds
        repo_inst.create_edge_if_absent = AsyncMock(side_effect=[RuntimeError("boom"), MagicMock()])

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=cfg),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                new=MagicMock(return_value=repo_inst),
            ),
        ):
            # Must not raise — best-effort error swallowing.
            await ms._create_tag_cooccurrence_seed_edges(session, base_memory)

        assert repo_inst.create_edge_if_absent.await_count == 2


class TestForget:
    """Test forget (delete) operations."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_forget_by_id_not_found(self, service):
        """forget() with nonexistent memory returns empty response (no exception)."""
        memory_id = uuid4()
        request = ForgetRequest(memory_id=memory_id)
        # _get_context_isolation_params calls context_service.get_context only when
        # current_context_id is provided; here it is None so returns (None, None, None)
        service.memory_repo.get = AsyncMock(return_value=None)
        service.db.commit = AsyncMock()

        response = await service.forget(request=request, user_id="test_user")
        assert response.deleted_count == 0
        assert response.memory_ids == []


class TestExploreHints:
    """Test explore_hints generation in recall (Issue #216)."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    def _make_mock_memory(self, memory_id=None, summary="Test", tags=None):
        m = MagicMock()
        m.id = memory_id or str(uuid4())
        m.summary = summary
        m.context_summary = None
        m.type = "note"
        m.importance = 0.7
        m.scope = "persistent"
        m.created_at = datetime.utcnow()
        m.last_used_at = datetime.utcnow()
        m.access_count = 1
        m.confidence = 0.8
        m.client = "test"
        m.tags = tags or []
        m.context = None
        m.source_uri = None
        m.source_type = None
        return m

    @pytest.mark.asyncio
    async def test_recall_no_hints_when_opt_out(self, service, context_id, workspace_id):
        """When include_explore_hints=False (default), explore_hints is None."""
        request = RecallRequest(query="test", k=5)
        mid = str(uuid4())

        service.search_service.hybrid_search = AsyncMock(
            return_value=[{"id": mid, "score": 0.9, "hybrid_score": 0.9}]
        )
        mock_mem = self._make_mock_memory(memory_id=mid)
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_mem]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        service.db.execute = AsyncMock(return_value=mock_result)
        service.db.commit = AsyncMock()
        service.memory_repo.update_access_stats = AsyncMock()
        service._check_and_promote = AsyncMock()

        response = await service.recall(
            request=request,
            user_id="test_user",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
        )

        assert response.explore_hints is None

    @pytest.mark.asyncio
    async def test_recall_hints_with_opt_in(self, service, context_id, workspace_id):
        """When include_explore_hints=True, at least top_result hint is returned."""
        request = RecallRequest(query="test", k=5, include_explore_hints=True)
        mid = str(uuid4())

        service.search_service.hybrid_search = AsyncMock(
            return_value=[{"id": mid, "score": 0.9, "hybrid_score": 0.9}]
        )
        mock_mem = self._make_mock_memory(memory_id=mid)
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_mem]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        service.db.execute = AsyncMock(return_value=mock_result)
        service.db.commit = AsyncMock()
        service.memory_repo.update_access_stats = AsyncMock()
        service._check_and_promote = AsyncMock()

        with patch.dict("os.environ", {"ENABLE_NEURAL_MEMORY": "true"}):
            with patch("repositories.neural_edge.NeuralEdgeRepository") as MockEdgeRepo:
                mock_repo = MockEdgeRepo.return_value
                mock_repo.get_node_degree = AsyncMock(return_value=(2, 3))

                response = await service.recall(
                    request=request,
                    user_id="test_user",
                    current_context_id=context_id,
                    current_workspace_id=workspace_id,
                )

        assert response.explore_hints is not None
        assert len(response.explore_hints) >= 1
        assert response.explore_hints[0].reason == "top_result"

    @pytest.mark.asyncio
    async def test_recall_hints_empty_when_no_results(self, service, context_id, workspace_id):
        """When include_explore_hints=True but no results, hints are empty."""
        request = RecallRequest(query="nothing", k=5, include_explore_hints=True)

        mock_context = MagicMock(
            id=context_id,
            workspace_id=workspace_id,
            is_private=True,
            created_by="test_user",
        )
        service.context_service.get_context = AsyncMock(return_value=mock_context)

        service.search_service.hybrid_search = AsyncMock(return_value=[])

        response = await service.recall(
            request=request,
            user_id="test_user",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
        )

        assert response.explore_hints is None or response.explore_hints == []

    @pytest.mark.asyncio
    async def test_recall_hints_failure_does_not_fail_recall(
        self, service, context_id, workspace_id
    ):
        """Hint generation failures are swallowed — recall still succeeds."""
        request = RecallRequest(query="test", k=5, include_explore_hints=True)
        mid = str(uuid4())

        service.search_service.hybrid_search = AsyncMock(
            return_value=[{"id": mid, "score": 0.9, "hybrid_score": 0.9}]
        )
        mock_mem = self._make_mock_memory(memory_id=mid)
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_mem]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        service.db.execute = AsyncMock(return_value=mock_result)
        service.db.commit = AsyncMock()
        service.memory_repo.update_access_stats = AsyncMock()
        service._check_and_promote = AsyncMock()

        with patch.dict("os.environ", {"ENABLE_NEURAL_MEMORY": "true"}):
            with patch("repositories.neural_edge.NeuralEdgeRepository") as MockEdgeRepo:
                mock_repo = MockEdgeRepo.return_value
                mock_repo.get_node_degree = AsyncMock(side_effect=Exception("DB error"))

                response = await service.recall(
                    request=request,
                    user_id="test_user",
                    current_context_id=context_id,
                    current_workspace_id=workspace_id,
                )

        # Recall succeeded despite hint failure
        assert response.results is not None
        assert len(response.results) == 1


class TestExploreAccessStats:
    """Issue #644: explore() bumps access_count / last_used_at on returned memories
    consistent with recall() and reference()."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_explore_bumps_seed_when_seed_not_in_graph(self, service):
        """Path A: has_node() returns False → only seed is bumped, then return."""
        from models.schemas import ExploreRequest

        seed_id = uuid4()
        workspace_id = uuid4()
        context_id = uuid4()

        mock_seed = MagicMock(
            id=seed_id,
            user_id="test_user",
            summary="Seed",
            context_summary=None,
            type="note",
            importance=0.5,
            scope="working",
            created_at=datetime.utcnow(),
            client="api",
            tags=[],
            context=None,
            workspace_id=workspace_id,
            context_id=context_id,
        )
        service.memory_repo.get = AsyncMock(return_value=mock_seed)
        service.memory_repo.update_access_stats = AsyncMock()
        service.db.commit = AsyncMock()

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch("repositories.graph.GraphRepository") as mock_graph_repo_cls,
            patch("services.graph_service.GraphService") as mock_graph_service_cls,
        ):
            mock_perm = MagicMock()
            mock_perm.can_access_memory = AsyncMock(return_value=True)
            mock_perm_cls.return_value = mock_perm

            mock_graph_repo = MagicMock()
            mock_graph_repo.get_or_create = AsyncMock()
            mock_graph_repo_cls.return_value = mock_graph_repo

            mock_graph_service = MagicMock()
            mock_graph_service.has_node = AsyncMock(return_value=False)
            mock_graph_service_cls.return_value = mock_graph_service

            response = await service.explore(
                request=ExploreRequest(memory_id=seed_id, depth=2),
                user_id="test_user",
            )

        assert response.metadata.get("reason") == "seed_not_in_graph"
        assert response.related_memories == []

        # Only one update_access_stats call: for the seed, with client="api".
        service.memory_repo.update_access_stats.assert_awaited_once_with(seed_id, client="api")
        service.db.commit.assert_awaited()
