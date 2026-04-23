"""Tests for k-NN cold-start seeding (Issue #221).

Verifies the `_create_knn_seed_edges` helper that runs after Qdrant upsert
in `process_pending_embedding` to bootstrap the Neural Memory graph for
new memories.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.memory_service import _create_knn_seed_edges


def _make_memory(workspace_id=None, context_id=None, user_id="user1"):
    """Build a Memory mock with the fields _create_knn_seed_edges reads."""
    memory = MagicMock()
    memory.id = uuid4()
    memory.user_id = user_id
    memory.workspace_id = workspace_id or uuid4()
    memory.context_id = context_id or uuid4()
    return memory


def _make_config(
    enabled=True,
    k=5,
    min_similarity=0.4,
    weight=0.3,
):
    """Build a NeuralMemoryConfig mock."""
    config = MagicMock()
    config.knn_seed_enabled = enabled
    config.knn_seed_k = k
    config.knn_seed_min_similarity = min_similarity
    config.knn_seed_weight = weight
    return config


def _make_db():
    """Build an AsyncSession mock.

    `begin_nested()` is the SAVEPOINT context manager used around each edge
    insert. The mock returns an async context manager that is a no-op for
    the success path; tests that simulate insert failure raise inside the
    with-block so the outer try/except catches it (matching real behavior).
    """
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    # begin_nested() returns an async context manager
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=nested_cm)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=nested_cm)

    return db


def _make_edge_repo(existing_edges=None, created_edge=None):
    """Build a NeuralEdgeRepository mock.

    Args:
        existing_edges: Return value for get_outgoing_edges (idempotency guard).
            Default [] = no existing edges = new memory, seeding proceeds.
            Non-empty = already seeded, seeding skipped.
        created_edge: Return value for create_edge_if_absent.
            Default MagicMock() simulates successful insert.
            None simulates ON CONFLICT DO NOTHING (edge already existed).
    """
    repo = MagicMock()
    repo.get_outgoing_edges = AsyncMock(return_value=existing_edges or [])
    repo.create_edge_if_absent = AsyncMock(
        return_value=created_edge if created_edge is not None else MagicMock()
    )
    return repo


class TestKnnSeeding:
    """Unit tests for _create_knn_seed_edges."""

    @pytest.mark.asyncio
    async def test_disabled_flag_skips_search(self):
        """When knn_seed_enabled=False, no Qdrant search happens."""
        memory = _make_memory()
        db = _make_db()

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(enabled=False)),
            ),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock()) as mock_search,
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        mock_search.assert_not_called()
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_context_creates_no_edges(self):
        """When Qdrant returns no neighbors, no edges are created."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        mock_repo.create_edge_if_absent.assert_not_called()
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_exclusion(self):
        """The new memory itself appears in Qdrant results and must be filtered out."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()

        # Qdrant returns the new memory + 2 real neighbors
        candidates = [
            {"id": str(memory.id), "score": 1.0, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.85, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.75, "payload": {}, "embedding": []},
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(k=5)),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        # 2 edges created (self excluded)
        assert mock_repo.create_edge_if_absent.call_count == 2
        # Verify self_id never appears as dst_id
        for call in mock_repo.create_edge_if_absent.call_args_list:
            assert call.kwargs["dst_id"] != memory.id

    @pytest.mark.asyncio
    async def test_threshold_filter_excludes_low_similarity(self):
        """Neighbors with score < min_similarity are excluded."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()

        candidates = [
            {"id": str(uuid4()), "score": 0.9, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.7, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.5, "payload": {}, "embedding": []},  # below 0.6
            {"id": str(uuid4()), "score": 0.4, "payload": {}, "embedding": []},  # below 0.6
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(min_similarity=0.6)),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        # Only 2 edges (0.9 and 0.7 pass the 0.6 threshold)
        assert mock_repo.create_edge_if_absent.call_count == 2

    @pytest.mark.asyncio
    async def test_k_limit_caps_edge_count(self):
        """No more than k edges are created even if more candidates qualify."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()

        # 10 high-score candidates
        candidates = [
            {"id": str(uuid4()), "score": 0.9 - i * 0.01, "payload": {}, "embedding": []}
            for i in range(10)
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(k=3)),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        # Capped at k=3
        assert mock_repo.create_edge_if_absent.call_count == 3

    @pytest.mark.asyncio
    async def test_edge_metadata_correct(self):
        """Edges are created with correct type, weight, confidence, isolation params."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()

        neighbor_id = uuid4()
        candidates = [
            {"id": str(neighbor_id), "score": 0.85, "payload": {}, "embedding": []},
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(weight=0.3)),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        mock_repo.create_edge_if_absent.assert_called_once()
        call = mock_repo.create_edge_if_absent.call_args
        assert call.kwargs["src_id"] == memory.id
        assert call.kwargs["dst_id"] == neighbor_id
        assert call.kwargs["edge_type"] == "semantic_similarity"
        assert call.kwargs["weight"] == 0.3
        assert call.kwargs["confidence"] == 0.85
        assert call.kwargs["workspace_id"] == str(memory.workspace_id)
        assert call.kwargs["context_id"] == str(memory.context_id)
        assert call.kwargs["user_id"] == memory.user_id

    @pytest.mark.asyncio
    async def test_search_uses_same_workspace_and_context(self):
        """Qdrant search must be scoped to memory's workspace+context (isolation)."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=[]),
            ) as mock_search,
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        mock_search.assert_called_once()
        call = mock_search.call_args
        assert call.kwargs["workspace_id"] == str(memory.workspace_id)
        assert call.kwargs["context_id"] == str(memory.context_id)
        assert call.kwargs["user_id"] == memory.user_id
        assert call.kwargs["limit"] == 6  # k=5 + 1 for self

    @pytest.mark.asyncio
    async def test_qdrant_failure_swallowed_best_effort(self):
        """If Qdrant search raises, the function does NOT raise (best-effort)."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(side_effect=RuntimeError("Qdrant down")),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            # Must not raise
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        # Rollback called as part of best-effort cleanup
        db.rollback.assert_called()

    @pytest.mark.asyncio
    async def test_edge_creation_failure_does_not_block_other_edges(self):
        """If one edge insert raises a DB error, SAVEPOINT isolates it and the
        remaining neighbors still get processed."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        # 1st and 3rd return a created edge, 2nd raises (SQLAlchemyError-like)
        mock_repo.create_edge_if_absent = AsyncMock(
            side_effect=[MagicMock(), RuntimeError("DB error"), MagicMock()]
        )

        candidates = [
            {"id": str(uuid4()), "score": 0.9, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.8, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.7, "payload": {}, "embedding": []},
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        # All 3 attempts made
        assert mock_repo.create_edge_if_absent.call_count == 3
        # Commit called once (after the loop, since at least 1 succeeded)
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_workspace_or_context_skips(self):
        """If memory lacks workspace_id or context_id, seeding is skipped (defensive)."""
        memory = _make_memory()
        memory.workspace_id = None
        db = _make_db()

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock()) as mock_search,
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        mock_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_conflict_do_nothing_not_counted_as_created(self):
        """When create_edge_if_absent returns None (edge already existed, ON
        CONFLICT DO NOTHING fired), the edge is preserved and not counted
        toward edges_created. commit() is not called if no new edges."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        # Simulate all 2 candidates already having edges (existing Hebbian edges)
        mock_repo.create_edge_if_absent = AsyncMock(return_value=None)

        candidates = [
            {"id": str(uuid4()), "score": 0.9, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.8, "payload": {}, "embedding": []},
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        # All 2 attempted, but none counted as "created" since DO NOTHING fired
        assert mock_repo.create_edge_if_absent.call_count == 2
        # commit() NOT called because nothing was actually inserted
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_savepoint_used_per_edge_insert(self):
        """Each edge insert is wrapped in db.begin_nested() (SAVEPOINT)
        so that a DB error on one edge does not corrupt the outer session."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()

        candidates = [
            {"id": str(uuid4()), "score": 0.9, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.8, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.7, "payload": {}, "embedding": []},
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        # begin_nested() called once per edge attempt (3 candidates)
        assert db.begin_nested.call_count == 3

    @pytest.mark.asyncio
    async def test_idempotent_skip_when_edges_already_exist(self):
        """Idempotency guard: if memory already has outgoing edges, skip seeding.

        This prevents update_memory() re-embeds from overwriting existing
        Hebbian-learned edges via ON CONFLICT DO UPDATE on the
        (user_id, src_id, dst_id) unique constraint.
        """
        memory = _make_memory()
        db = _make_db()

        # Simulate an existing edge (e.g., from prior Hebbian learning)
        existing_edge = MagicMock()
        mock_repo = _make_edge_repo(existing_edges=[existing_edge])

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(),
            ) as mock_search,
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        # Idempotency guard: get_outgoing_edges was called, returned existing edges,
        # so Qdrant search and edge creation are skipped
        mock_repo.get_outgoing_edges.assert_called_once()
        mock_search.assert_not_called()
        mock_repo.create_edge_if_absent.assert_not_called()
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_threshold_skips_search_and_triggers_bootstrap(self):
        """D4 step 3: ``resolve_knn_threshold`` → None disables seeding
        for this call and invokes ``maybe_trigger_bootstrap`` (best-effort).

        Guards the Phase B fallback-chain wiring in ``_create_knn_seed_edges``
        so a regression that silently skips the bootstrap trigger (or keeps
        issuing Qdrant searches with a broken threshold) is caught early.
        (Copilot review PR #420 loop 6.)
        """
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=None),
            ) as mock_resolve,
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(),
            ) as mock_search,
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "tasks.neural_calibration.maybe_trigger_bootstrap",
                new=AsyncMock(return_value=True),
            ) as mock_bootstrap,
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        # Threshold resolver was consulted with the right dimensions.
        mock_resolve.assert_awaited_once()
        # No Qdrant search: step 3 disables seeding for this call.
        mock_search.assert_not_called()
        # No edges created.
        mock_repo.create_edge_if_absent.assert_not_called()
        # Bootstrap trigger WAS invoked — subsequent remember() calls can
        # find a populated calibration row once D3 is crossed.
        mock_bootstrap.assert_awaited_once()
        # dimensions argument plumbed correctly (len(vector) = 512)
        call_kwargs = mock_bootstrap.await_args.kwargs
        assert call_kwargs["model_name"] == "text-embedding-3-small"
        assert call_kwargs["dimensions"] == 512
