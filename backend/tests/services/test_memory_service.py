"""Tests for MemoryService."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import (
    ForgetRequest,
    PatchMemoryRequest,
    RecallRequest,
    RememberRequest,
    UpdateMemoryRequest,
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
    async def test_reference_soft_deleted_raises_not_found(self, service):
        """#1316: repo.get() excludes tombstones by default and reference()
        must NOT opt in — a forgotten memory is uniformly not found by
        direct-id fetch during the retention window."""
        from utils.exceptions import NotFoundException

        memory_id = uuid4()
        # The real repo filters deleted_at IS NULL by default → None.
        service.memory_repo.get = AsyncMock(return_value=None)

        with pytest.raises(NotFoundException):
            await service.reference(memory_id=memory_id, user_id="test_user")

        # Pins that reference() does not pass include_deleted=True.
        service.memory_repo.get.assert_awaited_once_with(memory_id)

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

    @pytest.mark.asyncio
    async def test_reference_records_adoption_signal(self, service):
        """Issue #1046: reference() bumps the adoption signal (count_as_adoption=True),
        distinct from surfacing call sites (recall/explore) which leave it False."""
        memory_id = uuid4()
        mock_memory = MagicMock(
            id=memory_id,
            user_id="test_user",
            summary="Test",
            content="Test content",
            context_summary="Context",
            details={},
            type="code",
            importance=0.8,
            tags=[],
            context=None,
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

            mock_edge_repo = MagicMock()
            mock_edge_repo.get_outgoing_edges = AsyncMock(return_value=[])
            mock_edge_repo.get_incoming_edges = AsyncMock(return_value=[])
            mock_edge_cls.return_value = mock_edge_repo

            await service.reference(memory_id=memory_id, user_id="test_user")

        service.memory_repo.update_access_stats.assert_awaited_once_with(
            memory_id, client="api", count_as_adoption=True
        )


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

    @pytest.mark.asyncio
    async def test_forget_by_id_already_deleted_counts_zero(self, service):
        """#1320: a second forget of the same id must report deleted_count=0
        and must not re-stamp deleted_at/deleted_by. repo.get() excludes
        tombstones by default and forget() must NOT opt in."""
        memory_id = uuid4()
        # The real repo filters deleted_at IS NULL by default → None.
        service.memory_repo.get = AsyncMock(return_value=None)
        service.memory_repo.update = AsyncMock()
        service.db.commit = AsyncMock()

        response = await service.forget(
            request=ForgetRequest(memory_id=memory_id), user_id="test_user"
        )

        assert response.deleted_count == 0
        assert response.memory_ids == []
        service.memory_repo.update.assert_not_awaited()
        # Pins that forget() does not pass include_deleted=True.
        service.memory_repo.get.assert_awaited_once_with(memory_id)


class TestUpdateInPlaceTombstone:
    """#1316 review sweep: _update_in_place relies on the repo default filter."""

    @pytest.mark.asyncio
    async def test_update_does_not_opt_into_tombstones(self):
        """Opting in (include_deleted=True) here would let an edit stamp new
        content and re-embed onto a still-tombstoned row (searchable-but-
        deleted inconsistency) — pin the default-filtered fetch."""
        from utils.exceptions import NotFoundException

        service = MemoryService(MagicMock())
        service.memory_repo.get = AsyncMock(return_value=None)
        request = UpdateMemoryRequest(
            memory_id=uuid4(), summary="a new summary long enough for schema"
        )

        with pytest.raises(NotFoundException):
            await service._update_in_place(request, user_id="test_user")

        service.memory_repo.get.assert_awaited_once_with(request.memory_id)


class TestGetContextIsolationParamsKeyWorkspaceConfinement:
    """Issue #963/#1281 item 2: pure API-key workspace-scope confinement on the
    declared-context path (remember / load_pinned / forget), mirroring the MCP
    _resolve_context check."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @staticmethod
    def _ctx(ws_id):
        c = MagicMock()
        c.workspace_id = ws_id
        return c

    @pytest.mark.asyncio
    async def test_foreign_key_scope_denies_as_not_found(self, service):
        # Workspace-scoped key (key_ws) declaring a context owned by another
        # workspace (ctx_ws) the member also belongs to → uniform NotFound.
        from utils.exceptions import NotFoundException

        key_ws, ctx_ws, ctx_id = uuid4(), uuid4(), uuid4()
        service.context_service.get_context = AsyncMock(return_value=self._ctx(ctx_ws))
        with patch(
            "services.agent_binding_service.agent_binding_permits",
            new=AsyncMock(return_value=True),
        ):
            with pytest.raises(NotFoundException):
                await service._get_context_isolation_params(
                    "u", ctx_id, access="write", key_workspace_id=key_ws
                )

    @pytest.mark.asyncio
    async def test_matching_key_scope_allows(self, service):
        ws, ctx_id = uuid4(), uuid4()
        service.context_service.get_context = AsyncMock(return_value=self._ctx(ws))
        with patch(
            "services.agent_binding_service.agent_binding_permits",
            new=AsyncMock(return_value=True),
        ):
            _ctx, ws_str, cid_str = await service._get_context_isolation_params(
                "u", ctx_id, access="write", key_workspace_id=ws
            )
        assert ws_str == str(ws)
        assert cid_str == str(ctx_id)

    @pytest.mark.asyncio
    async def test_no_key_scope_skips_confinement(self, service):
        # OAuth/session/global-key: scope None → a foreign workspace is NOT
        # confined here (over-confinement guard).
        ctx_ws, ctx_id = uuid4(), uuid4()
        service.context_service.get_context = AsyncMock(return_value=self._ctx(ctx_ws))
        with patch(
            "services.agent_binding_service.agent_binding_permits",
            new=AsyncMock(return_value=True),
        ):
            _ctx, ws_str, _cid = await service._get_context_isolation_params(
                "u", ctx_id, key_workspace_id=None
            )
        assert ws_str == str(ctx_ws)


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
        m.updated_at = datetime.utcnow()  # #1047: staleness cue
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

        # Issue #1047: recall always populates a confidence signal, and results
        # carry the updated_at staleness cue.
        assert response.confidence is not None
        assert response.confidence.level in {"high", "moderate", "low", "none"}
        assert response.confidence.result_count == 1
        assert response.results[0].updated_at is not None

        # Issue #1046: recall is *surfacing*, not adoption — it bumps access_count
        # but MUST NOT record the adoption signal. No call may set
        # count_as_adoption=True (the default False keeps reference_count untouched).
        assert service.memory_repo.update_access_stats.await_count >= 1
        for call in service.memory_repo.update_access_stats.await_args_list:
            assert call.kwargs.get("count_as_adoption") is not True

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


class TestRecallConfidence:
    """Issue #1047: the FALLBACK (keyword-only / no raw semantic cosines) path —
    per-context-relative z-score separation. Since #1052 this is only the fallback;
    the main hybrid/semantic path is covered by TestRecallConfidenceSemantic."""

    def test_empty_is_none(self):
        c = MemoryService._compute_recall_confidence([])
        assert c.level == "none"
        assert c.result_count == 0
        assert c.top_score is None
        assert c.relative_margin is None

    def test_single_result_is_moderate_no_margin(self):
        c = MemoryService._compute_recall_confidence([0.8])
        assert c.level == "moderate"
        assert c.result_count == 1
        assert c.relative_margin is None

    def test_clear_separation_is_high(self):
        c = MemoryService._compute_recall_confidence([0.95, 0.30, 0.31, 0.29, 0.30])
        assert c.level == "high"
        assert c.relative_margin is not None and c.relative_margin >= 2.0

    def test_flat_background_top_indistinguishable_is_none(self):
        # All scores ~equal → top doesn't stand out from background → "none".
        c = MemoryService._compute_recall_confidence([0.50, 0.50, 0.50, 0.50])
        assert c.level == "none"

    def test_scale_invariance_not_absolute_cutoff(self):
        # Fallback path only: same separation SHAPE at very different absolute
        # magnitudes yields the SAME level (z-score is scale-invariant). Note the
        # MAIN semantic path (#1052) deliberately is NOT scale-invariant in this
        # way — it weighs absolute cosine — see TestRecallConfidenceSemantic.
        low_scale = MemoryService._compute_recall_confidence([0.20, 0.05, 0.06, 0.04, 0.05])
        high_scale = MemoryService._compute_recall_confidence([0.95, 0.80, 0.81, 0.79, 0.80])
        assert low_scale.level == high_scale.level == "high"


class TestRecallConfidenceSemantic:
    """Issue #1052: the MAIN path — ``level`` from RAW semantic cosines so an
    off-topic ("absent") query is NOT reported as relevant. Distributions below
    mirror a live 398-memory benchmark (real embeddings, 2026-06-20)."""

    # --- present (on-topic): high absolute cosine, clearly prominent ---
    PRESENT = [0.919, 0.676, 0.670, 0.615, 0.608, 0.537, 0.520, 0.500]
    # --- absent (off-topic): mediocre top, flat cluster just above background ---
    ABSENT = [0.645, 0.572, 0.572, 0.561, 0.560, 0.531, 0.510, 0.500]

    def _c(self, sem):
        # Pass scores=[] so result_count derives from the semantic pool.
        return MemoryService._compute_recall_confidence([], semantic_scores=sem)

    def test_present_is_high(self):
        c = self._c(self.PRESENT)
        assert c.level == "high"
        assert c.top_score == 0.919  # raw cosine, not the normalized hybrid 0.6
        assert c.prominence is not None and c.prominence >= 0.40

    def test_absent_is_not_relevant(self):
        # The core #1052 fix: the OLD z-score margin called this "high" because the
        # top barely separates from a flat low tail; absolute strength reveals it.
        c = self._c(self.ABSENT)
        assert c.level in ("low", "none")
        # And the misleading z-margin is in fact high here — proving why level
        # must not rely on it.
        assert c.relative_margin is not None and c.relative_margin >= 2.0

    def test_high_zmargin_but_low_absolute_is_downgraded(self):
        # Top sits well above a perfectly flat tail → huge z-margin → the old code
        # said "high". New code: prominence (0.60-0.55)/0.55 ≈ 0.09 → "none".
        c = self._c([0.60, 0.55, 0.55, 0.55, 0.55, 0.55])
        assert c.level == "none"

    def test_near_duplicate_low_spread_is_rescued(self):
        # Low-spread model: cosines clustered high → small prominence, but a 0.90
        # top is a near-duplicate match and must not read as absent.
        c = self._c([0.90, 0.88, 0.88, 0.87, 0.88])
        assert c.level == "moderate"

    def test_semantic_takes_precedence_over_hybrid_fallback(self):
        # Hybrid scores alone (flat 0.6) would be "none" via the fallback; raw
        # semantic must drive the verdict instead.
        c = MemoryService._compute_recall_confidence(
            [0.6, 0.6, 0.6, 0.6], semantic_scores=self.PRESENT
        )
        assert c.level == "high"
        assert c.result_count == 4  # from the hybrid pool length

    def test_single_strong_semantic_is_moderate(self):
        c = self._c([0.91])
        assert c.level == "moderate"
        assert c.prominence is None

    def test_single_weak_semantic_is_low(self):
        c = self._c([0.40])
        assert c.level == "low"

    def test_top_score_is_raw_cosine(self):
        c = self._c([0.873, 0.4, 0.39, 0.38])
        assert c.top_score == 0.873

    def test_zero_or_negative_background_weak_top_is_not_relevant(self):
        # Qdrant returns raw cosines with no score_threshold, so an off-topic
        # query can produce a ~zero/negative background mean. The ratio is
        # meaningless there; a weak absolute top must NOT be forced to "high".
        c = self._c([0.05, -0.20, -0.30, -0.25])
        assert c.level == "none"
        assert c.prominence is None  # no usable relative frame

    def test_negative_background_strong_top_is_high(self):
        # Same degenerate background, but a near-duplicate top is still detected.
        c = self._c([0.90, -0.10, 0.00, -0.05])
        assert c.level == "high"

    def test_weak_top_over_weaker_background_not_high(self):
        # Pure ratio would call this "high" (prominence = (0.30-0.10)/0.10 = 2.0),
        # but an absolute top of 0.30 is a weak match — the absolute floor caps it.
        c = self._c([0.30, 0.10, 0.10, 0.10])
        assert c.level != "high"
        assert c.level == "moderate"


class TestReinforceFactor:
    """Issue #1048: bounded, importance-weighted reinforce factor — the 2-population
    eval guard (adopted improves; rare-but-correct does not regress; popularity is
    bounded; cold-start surfaces new zero-adoption memories)."""

    MAX = 0.15

    def _f(self, **kw):
        kw.setdefault("max_boost", self.MAX)
        return MemoryService._reinforce_factor(**kw)

    def test_bounded_within_max_boost(self):
        hi = self._f(reference_count=10_000, net_helpful=1000, importance=1.0, age_days=0)
        lo = self._f(reference_count=0, net_helpful=-1000, importance=1.0, age_days=0)
        assert (1 - self.MAX) - 1e-9 <= lo <= hi <= (1 + self.MAX) + 1e-9

    def test_known_value_pins_formula(self):
        # Zero adoption, no feedback, importance 0.5, fresh → cold prior = 0.25 →
        # factor = 1 + 0.15 * (0 + 0.25). Pins the actual formula, not just the clamp.
        f = self._f(reference_count=0, net_helpful=0, importance=0.5, age_days=0)
        assert abs(f - 1.0375) < 1e-9

    def test_adopted_and_helpful_boosts(self):
        assert self._f(reference_count=5, net_helpful=5, importance=0.8, age_days=100) > 1.0

    def test_not_helpful_penalizes(self):
        assert self._f(reference_count=0, net_helpful=-5, importance=0.8, age_days=100) < 1.0

    def test_rare_but_correct_not_regressed(self):
        # Zero adoption, no feedback, high importance, OLD → neutral (>=1): never
        # demoted without explicit not-helpful feedback (the rare-but-correct guard).
        assert self._f(reference_count=0, net_helpful=0, importance=0.9, age_days=365) >= 1.0

    def test_cold_start_surfaces_new_zero_adoption(self):
        fresh = self._f(reference_count=0, net_helpful=0, importance=0.5, age_days=0)
        old = self._f(reference_count=0, net_helpful=0, importance=0.5, age_days=365)
        assert fresh > old >= 1.0

    def test_not_purely_usage_monotonic(self):
        # A fresh zero-adoption memory outranks an old, barely-important one —
        # the cold-start term makes the boost NOT purely usage-monotonic.
        fresh_unadopted = self._f(reference_count=0, net_helpful=0, importance=0.5, age_days=0)
        old_trivial = self._f(reference_count=0, net_helpful=0, importance=0.01, age_days=365)
        assert fresh_unadopted > old_trivial

    def test_popularity_bias_capped_by_importance(self):
        # A super-popular but TRIVIAL memory must not out-boost a rare, important,
        # recent one (importance-weighting + adoption cap).
        popular_trivial = self._f(
            reference_count=10_000, net_helpful=100, importance=0.05, age_days=200
        )
        rare_important_recent = self._f(
            reference_count=0, net_helpful=0, importance=0.9, age_days=0
        )
        assert rare_important_recent >= popular_trivial


class TestReinforceRerank:
    """Issue #1048: _maybe_reinforce_rerank is config-gated and only reorders the
    relevance-filtered pool within the bound. (#1207: the per-context default is
    now ON for new/materialized config rows; a stored explicit ``false`` opts
    out — each test below pins the gate state via an explicit mock config.)"""

    @pytest.mark.asyncio
    async def test_noop_when_disabled(self):
        svc = MemoryService(MagicMock())
        cfg = MagicMock(reinforce_enabled=False)
        sr = [{"id": "a", "hybrid_score": 0.9}, {"id": "b", "hybrid_score": 0.8}]
        with patch("repositories.config_repository.ContextSearchConfigRepository") as Repo:
            Repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            await svc._maybe_reinforce_rerank(sr, {}, uuid4())
        assert [r["id"] for r in sr] == ["a", "b"]  # unchanged

    @pytest.mark.asyncio
    async def test_reorders_within_bound_when_enabled(self):
        from decimal import Decimal

        from services.feedback_service import FeedbackAggregate

        svc = MemoryService(MagicMock())
        cfg = MagicMock(
            reinforce_enabled=True,
            reinforce_max_boost=Decimal("0.15"),
            reinforce_require_host_arbitration=False,
        )
        old = datetime(2020, 1, 1)
        mem_a = MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=old)
        mem_b = MagicMock(id=uuid4(), reference_count=10, importance=0.9, created_at=old)
        memories = {"a": mem_a, "b": mem_b}
        # a has the higher raw hybrid, but b is heavily adopted + helpful.
        sr = [{"id": "a", "hybrid_score": 0.85}, {"id": "b", "hybrid_score": 0.80}]
        with (
            patch("repositories.config_repository.ContextSearchConfigRepository") as Repo,
            patch("services.feedback_service.FeedbackService") as FB,
        ):
            Repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            FB.return_value.aggregate_for_memories = AsyncMock(
                return_value={
                    str(mem_b.id): FeedbackAggregate(
                        memory_id=str(mem_b.id), helpful_count=5, not_helpful_count=0
                    )
                }
            )
            await svc._maybe_reinforce_rerank(sr, memories, uuid4())
        # The adopted+helpful b overtakes a (within the bound).
        assert [r["id"] for r in sr][0] == "b"

    @pytest.mark.asyncio
    async def test_bound_keeps_clearly_more_relevant_on_top(self):
        from decimal import Decimal

        svc = MemoryService(MagicMock())
        cfg = MagicMock(
            reinforce_enabled=True,
            reinforce_max_boost=Decimal("0.15"),
            reinforce_require_host_arbitration=False,
        )
        old = datetime(2020, 1, 1)
        mem_a = MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=old)
        mem_b = MagicMock(id=uuid4(), reference_count=50, importance=1.0, created_at=old)
        memories = {"a": mem_a, "b": mem_b}
        # a is MUCH more relevant (0.95 vs 0.50); even max boost on b cannot overtake.
        sr = [{"id": "a", "hybrid_score": 0.95}, {"id": "b", "hybrid_score": 0.50}]
        with (
            patch("repositories.config_repository.ContextSearchConfigRepository") as Repo,
            patch("services.feedback_service.FeedbackService") as FB,
        ):
            Repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            FB.return_value.aggregate_for_memories = AsyncMock(return_value={})
            await svc._maybe_reinforce_rerank(sr, memories, uuid4())
        assert [r["id"] for r in sr][0] == "a"  # relevance still dominates

    @pytest.mark.asyncio
    async def test_require_host_arbitration_forwards_host_only(self):
        # Issue #1065: when forge-resistant mode is on, the actuator aggregates
        # ONLY host-arbitrated feedback (agent self-feedback cannot move ranking).
        from decimal import Decimal

        svc = MemoryService(MagicMock())
        cfg = MagicMock(
            reinforce_enabled=True,
            reinforce_max_boost=Decimal("0.15"),
            reinforce_require_host_arbitration=True,
        )
        old = datetime(2020, 1, 1)
        memories = {
            "a": MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=old),
            "b": MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=old),
        }
        sr = [{"id": "a", "hybrid_score": 0.9}, {"id": "b", "hybrid_score": 0.8}]
        agg = AsyncMock(return_value={})
        with (
            patch("repositories.config_repository.ContextSearchConfigRepository") as Repo,
            patch("services.feedback_service.FeedbackService") as FB,
        ):
            Repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            FB.return_value.aggregate_for_memories = agg
            await svc._maybe_reinforce_rerank(sr, memories, uuid4(), top_k=10)
        assert agg.await_args.kwargs.get("host_only") is True

    @pytest.mark.asyncio
    async def test_default_counts_all_feedback(self):
        # Default (flag off) keeps pre-#1065 behaviour: all feedback counts.
        from decimal import Decimal

        svc = MemoryService(MagicMock())
        cfg = MagicMock(
            reinforce_enabled=True,
            reinforce_max_boost=Decimal("0.15"),
            reinforce_require_host_arbitration=False,
        )
        old = datetime(2020, 1, 1)
        memories = {
            "a": MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=old),
            "b": MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=old),
        }
        sr = [{"id": "a", "hybrid_score": 0.9}, {"id": "b", "hybrid_score": 0.8}]
        agg = AsyncMock(return_value={})
        with (
            patch("repositories.config_repository.ContextSearchConfigRepository") as Repo,
            patch("services.feedback_service.FeedbackService") as FB,
        ):
            Repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            FB.return_value.aggregate_for_memories = agg
            await svc._maybe_reinforce_rerank(sr, memories, uuid4(), top_k=10)
        assert agg.await_args.kwargs.get("host_only") is False

    @pytest.mark.asyncio
    async def test_emits_telemetry_when_enabled(self):
        # Issue #1069: a fired re-rank emits one reinforce_rerank_applied event so a
        # staged rollout is observable; a disabled context emits nothing.
        from decimal import Decimal

        svc = MemoryService(MagicMock())
        cfg = MagicMock(
            reinforce_enabled=True,
            reinforce_max_boost=Decimal("0.15"),
            reinforce_require_host_arbitration=False,
        )
        old = datetime(2020, 1, 1)
        mem_a = MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=old)
        mem_b = MagicMock(id=uuid4(), reference_count=10, importance=0.9, created_at=old)
        memories = {"a": mem_a, "b": mem_b}
        sr = [{"id": "a", "hybrid_score": 0.85}, {"id": "b", "hybrid_score": 0.80}]
        with (
            patch("repositories.config_repository.ContextSearchConfigRepository") as Repo,
            patch("services.feedback_service.FeedbackService") as FB,
            patch("services.memory_service.logger") as log,
        ):
            Repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            FB.return_value.aggregate_for_memories = AsyncMock(return_value={})
            await svc._maybe_reinforce_rerank(sr, memories, uuid4(), top_k=10)
        events = [c.args[0] for c in log.info.call_args_list]
        assert events.count("reinforce_rerank_applied") == 1
        # The emitted summary carries the popularity-bias guard metric.
        kwargs = next(
            c.kwargs for c in log.info.call_args_list if c.args[0] == "reinforce_rerank_applied"
        )
        assert kwargs["candidates"] == 2
        assert "zero_adoption_in_topk" in kwargs

    @pytest.mark.asyncio
    async def test_no_telemetry_when_disabled(self):
        svc = MemoryService(MagicMock())
        cfg = MagicMock(reinforce_enabled=False)
        sr = [{"id": "a", "hybrid_score": 0.9}, {"id": "b", "hybrid_score": 0.8}]
        with (
            patch("repositories.config_repository.ContextSearchConfigRepository") as Repo,
            patch("services.memory_service.logger") as log,
        ):
            Repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            await svc._maybe_reinforce_rerank(sr, {}, uuid4())
        events = [c.args[0] for c in log.info.call_args_list]
        assert "reinforce_rerank_applied" not in events

    @pytest.mark.asyncio
    async def test_telemetry_failure_does_not_mask_applied_rerank(self):
        # A telemetry/log failure must NOT emit the misleading "skipped" signal the
        # rollout is monitored on (the re-rank already fired), and must not break
        # recall. It surfaces as a distinct reinforce_telemetry_failed instead.
        from decimal import Decimal

        svc = MemoryService(MagicMock())
        cfg = MagicMock(
            reinforce_enabled=True,
            reinforce_max_boost=Decimal("0.15"),
            reinforce_require_host_arbitration=False,
        )
        old = datetime(2020, 1, 1)
        mem_a = MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=old)
        mem_b = MagicMock(id=uuid4(), reference_count=10, importance=0.9, created_at=old)
        memories = {"a": mem_a, "b": mem_b}
        sr = [{"id": "a", "hybrid_score": 0.85}, {"id": "b", "hybrid_score": 0.80}]
        with (
            patch("repositories.config_repository.ContextSearchConfigRepository") as Repo,
            patch("services.feedback_service.FeedbackService") as FB,
            patch.object(MemoryService, "_reinforce_telemetry", side_effect=RuntimeError("boom")),
            patch("services.memory_service.logger") as log,
        ):
            Repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            FB.return_value.aggregate_for_memories = AsyncMock(return_value={})
            await svc._maybe_reinforce_rerank(sr, memories, uuid4(), top_k=10)
        warn_events = [c.args[0] for c in log.warning.call_args_list]
        assert "reinforce_telemetry_failed" in warn_events
        assert "reinforce_rerank_skipped" not in warn_events  # the re-rank DID fire
        assert {r["id"] for r in sr} == {"a", "b"}  # recall results intact


class TestReinforceTelemetry:
    """Issue #1069: the pure per-recall reinforce summary that makes enabling and any
    popularity-bias regression observable per context (structlog, no metrics backend)."""

    def _t(self, **kw):
        kw.setdefault("order_before", [])
        kw.setdefault("order_after", [])
        kw.setdefault("factors", {})
        kw.setdefault("zero_adoption_ids", set())
        kw.setdefault("top_k", 10)
        return MemoryService._reinforce_telemetry(**kw)

    def test_no_reorder_when_order_identical(self):
        t = self._t(order_before=["a", "b"], order_after=["a", "b"], factors={"a": 1.0, "b": 1.0})
        assert t["reordered"] is False
        assert t["top1_changed"] is False
        assert t["candidates"] == 2

    def test_detects_reorder_and_top1_change(self):
        t = self._t(order_before=["a", "b"], order_after=["b", "a"], factors={"a": 0.9, "b": 1.1})
        assert t["reordered"] is True
        assert t["top1_changed"] is True

    def test_reorder_without_top1_change(self):
        # Lower ranks shuffle but the head is stable → reordered, not top1_changed.
        t = self._t(
            order_before=["a", "b", "c"],
            order_after=["a", "c", "b"],
            factors={"a": 1.0, "b": 0.95, "c": 1.05},
        )
        assert t["reordered"] is True
        assert t["top1_changed"] is False

    def test_factor_distribution(self):
        t = self._t(
            order_before=["a", "b", "c"],
            order_after=["a", "b", "c"],
            factors={"a": 1.1, "b": 1.0, "c": 0.9},
        )
        assert abs(t["factor_min"] - 0.9) < 1e-9
        assert abs(t["factor_max"] - 1.1) < 1e-9
        assert abs(t["factor_mean"] - 1.0) < 1e-9
        assert t["boosted"] == 1
        assert t["demoted"] == 1

    def test_zero_adoption_surfacing_counted_within_topk_only(self):
        # z and w are both zero-adoption, but only z is inside the user-visible top-2.
        t = self._t(
            order_before=["z", "a", "w"],
            order_after=["z", "a", "w"],
            factors={"z": 1.03, "a": 1.0, "w": 1.03},
            zero_adoption_ids={"z", "w"},
            top_k=2,
        )
        assert t["zero_adoption_in_topk"] == 1
        assert t["topk"] == 2

    def test_empty_pool_is_safe(self):
        t = self._t()
        assert t["candidates"] == 0
        assert t["reordered"] is False
        assert t["top1_changed"] is False
        assert t["factor_min"] == 1.0
        assert t["factor_max"] == 1.0
        assert t["factor_mean"] == 1.0
        assert t["zero_adoption_in_topk"] == 0
        assert t["topk"] == 0


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
            deleted_at=None,
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

    @pytest.mark.asyncio
    async def test_explore_soft_deleted_seed_raises_not_found(self, service):
        """#1316: a forgotten memory must not surface as an exploration seed
        (previously leaked its summary as seed_not_in_graph). repo.get()
        excludes tombstones by default and explore() must NOT opt in; the
        message must match the access-denied shape (no existence oracle)."""
        from models.schemas import ExploreRequest
        from utils.exceptions import NotFoundException

        seed_id = uuid4()
        # The real repo filters deleted_at IS NULL by default → None.
        service.memory_repo.get = AsyncMock(return_value=None)

        with pytest.raises(NotFoundException) as exc_info:
            await service.explore(
                request=ExploreRequest(memory_id=seed_id, depth=2),
                user_id="test_user",
            )

        # Uniform two-arg shape — and never the doubled "not found not found".
        assert str(exc_info.value) == f"Memory not found: {seed_id}"
        service.memory_repo.get.assert_awaited_once_with(seed_id)


class TestAccessEventEmission:
    """#1286 item 2 (P0-5): update/patch/forget success-event emission.

    The emission lives in the service layer so REST and MCP get it for free
    (the writer stamps surface from the correlation contextvar and no-ops
    unless a verified AgentScope is set — these tests patch the writer, so
    they pin the CALL contract, not the writer's own gating).
    """

    @staticmethod
    def _memory_row(ws, ctx, mid):
        from datetime import datetime

        memory = MagicMock()
        memory.id = mid
        memory.user_id = "author"
        memory.workspace_id = ws
        memory.context_id = ctx
        memory.deleted_at = None
        memory.summary = "s"
        memory.context_summary = None
        memory.content = "c"
        memory.details = None
        memory.type = "note"
        memory.scope = "working"
        memory.importance = 0.5
        memory.tags = []
        memory.context = None
        memory.created_at = datetime(2026, 1, 1)
        memory.client = "test"
        memory.source_uri = None
        memory.source_type = None
        return memory

    @pytest.mark.asyncio
    async def test_update_in_place_emits_update_event(self):
        service = MemoryService(MagicMock())
        ws, ctx, mid = uuid4(), uuid4(), uuid4()
        service.memory_repo.get = AsyncMock(return_value=self._memory_row(ws, ctx, mid))
        service.db.commit = AsyncMock()
        service.db.flush = AsyncMock()

        with (
            patch(
                "services.permission_service.PermissionService.can_access_memory",
                AsyncMock(return_value=True),
            ) as can_access,
            patch("services.memory_service.resolve_collection_name", AsyncMock(return_value="c")),
            patch("services.memory_service.update_memory_payload_in_qdrant", AsyncMock()),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            res = await service.update_memory(
                UpdateMemoryRequest(memory_id=mid, tags=["x"]), "caller"
            )

        assert res.operation == "updated"
        emit.assert_awaited_once()
        kw = emit.await_args.kwargs
        assert kw["operation"] == "update"
        assert kw["outcome"] == "success"
        assert kw["workspace_id"] == ws
        assert kw["context_id"] == ctx
        assert kw["memory_id"] == mid
        assert kw["user_id"] == "caller"
        # #1286 deny-capture audit identity must reach the permission gate —
        # dropping it silently reverts deny rows for this op to log-only.
        perm_kw = can_access.await_args.kwargs
        assert perm_kw["operation"] == "update"
        assert perm_kw["memory_id"] == mid

    @pytest.mark.asyncio
    async def test_patch_memory_emits_update_event(self):
        # PATCH is the REST-side update surface (#439) — it must emit the
        # same operation="update" as MCP's _update_in_place (REST/MCP parity).
        service = MemoryService(MagicMock())
        ws, ctx, mid = uuid4(), uuid4(), uuid4()
        service.memory_repo.get = AsyncMock(return_value=self._memory_row(ws, ctx, mid))
        service.db.commit = AsyncMock()
        service.db.flush = AsyncMock()
        service._fetch_declared_link_refs = AsyncMock(return_value=([], False, [], False))

        with (
            patch(
                "services.permission_service.PermissionService.can_access_memory",
                AsyncMock(return_value=True),
            ),
            patch("services.memory_service.resolve_collection_name", AsyncMock(return_value="c")),
            patch("services.memory_service.update_memory_payload_in_qdrant", AsyncMock()),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            await service.patch_memory(mid, PatchMemoryRequest(tags=["x"]), "caller")

        emit.assert_awaited_once()
        kw = emit.await_args.kwargs
        assert kw["operation"] == "update"
        assert kw["outcome"] == "success"
        assert kw["workspace_id"] == ws
        assert kw["memory_id"] == mid

    @pytest.mark.asyncio
    async def test_forget_by_id_falls_back_to_memory_row_workspace(self):
        # REST forget declares no context (#246): the isolation helper
        # resolves no workspace there. The emission must fall back to the
        # deleted row's own (hard-validated) workspace, not silently no-op.
        service = MemoryService(MagicMock())
        ws, ctx, mid = uuid4(), uuid4(), uuid4()
        service.memory_repo.get = AsyncMock(return_value=self._memory_row(ws, ctx, mid))
        service.memory_repo.update = AsyncMock()
        service.db.commit = AsyncMock()

        with (
            patch(
                "services.permission_service.PermissionService.can_access_memory",
                AsyncMock(return_value=True),
            ) as can_access,
            patch("services.memory_service.resolve_collection_name", AsyncMock(return_value="c")),
            patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()),
            patch("repositories.neural_edge.NeuralEdgeRepository") as edge_cls,
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            edge_cls.return_value.delete_node_edges = AsyncMock(return_value=0)
            res = await service.forget(ForgetRequest(memory_id=mid), "caller")

        assert res.deleted_count == 1
        emit.assert_awaited_once()
        kw = emit.await_args.kwargs
        assert kw["operation"] == "forget"
        assert kw["outcome"] == "success"
        assert kw["workspace_id"] == ws
        assert kw["context_id"] == ctx
        assert kw["memory_id"] == mid
        assert kw["result_count"] == 1
        # #1286 deny-capture audit identity must reach the permission gate.
        perm_kw = can_access.await_args.kwargs
        assert perm_kw["operation"] == "forget"
        assert perm_kw["memory_id"] == mid

    @pytest.mark.asyncio
    async def test_forget_with_declared_context_uses_helper_workspace(self):
        # When a context IS declared (MCP always), the isolation helper's
        # workspace wins over the row's.
        service = MemoryService(MagicMock())
        ws_helper, ws_row, ctx, mid = uuid4(), uuid4(), uuid4(), uuid4()
        mock_context = MagicMock(id=ctx, workspace_id=ws_helper)
        service.context_service.get_context = AsyncMock(return_value=mock_context)
        service.memory_repo.get = AsyncMock(return_value=self._memory_row(ws_row, ctx, mid))
        service.memory_repo.update = AsyncMock()
        service.db.commit = AsyncMock()

        with (
            patch(
                "services.permission_service.PermissionService.can_access_memory",
                AsyncMock(return_value=True),
            ),
            patch("services.memory_service.resolve_collection_name", AsyncMock(return_value="c")),
            patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()),
            patch("repositories.neural_edge.NeuralEdgeRepository") as edge_cls,
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            edge_cls.return_value.delete_node_edges = AsyncMock(return_value=0)
            res = await service.forget(
                ForgetRequest(memory_id=mid), "caller", current_context_id=ctx
            )

        assert res.deleted_count == 1
        kw = emit.await_args.kwargs
        assert kw["workspace_id"] == ws_helper

    @pytest.mark.asyncio
    async def test_forget_denied_by_id_emits_no_success_event(self):
        # The silent-filter deny (empty success-shaped response) must not
        # produce a success audit row. (The denied row itself lands via the
        # can_access_memory deny-capture — pinned in the permission tests.)
        service = MemoryService(MagicMock())
        ws, ctx, mid = uuid4(), uuid4(), uuid4()
        service.memory_repo.get = AsyncMock(return_value=self._memory_row(ws, ctx, mid))

        with (
            patch(
                "services.permission_service.PermissionService.can_access_memory",
                AsyncMock(return_value=False),
            ),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            res = await service.forget(ForgetRequest(memory_id=mid), "caller")

        assert res.deleted_count == 0
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forget_by_query_multi_row_metadata_contract(self):
        # #1286 review finding: plural deletes must NOT stamp a single
        # authoritative memory_id — the ids ride event_metadata (capped at
        # MAX_METADATA_MEMORY_IDS) with result_count carrying the total.
        from types import SimpleNamespace

        service = MemoryService(MagicMock())
        ws, ctx = uuid4(), uuid4()
        ids = [uuid4(), uuid4()]
        rows = {mid: self._memory_row(ws, ctx, mid) for mid in ids}
        service.context_service.get_context = AsyncMock(
            return_value=MagicMock(id=ctx, workspace_id=ws)
        )
        service.recall = AsyncMock(
            return_value=SimpleNamespace(results=[SimpleNamespace(memory_id=m) for m in ids])
        )
        service.memory_repo.get = AsyncMock(side_effect=lambda m: rows.get(m))
        service.memory_repo.update = AsyncMock()
        service.db.commit = AsyncMock()

        with (
            patch("services.memory_service.resolve_collection_name", AsyncMock(return_value="c")),
            patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()),
            patch("repositories.neural_edge.NeuralEdgeRepository") as edge_cls,
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            edge_cls.return_value.delete_node_edges = AsyncMock(return_value=0)
            res = await service.forget(
                ForgetRequest(query="q", k=10), "caller", current_context_id=ctx
            )

        assert res.deleted_count == 2
        emit.assert_awaited_once()
        kw = emit.await_args.kwargs
        assert kw["memory_id"] is None  # plural: no single authoritative id
        assert kw["result_count"] == 2
        assert kw["extra_metadata"]["memory_ids"] == [str(m) for m in ids]
