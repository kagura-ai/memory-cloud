"""Tests for k-NN cold-start seeding (Issue #221).

Verifies the `_create_knn_seed_edges` helper that runs after Qdrant upsert
in `process_pending_embedding` to bootstrap the Neural Memory graph for
new memories.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services import memory_service
from services.memory_service import _create_knn_seed_edges


@pytest.fixture(autouse=True)
def _fresh_no_consumer_reported(monkeypatch):
    """#2023: whether ``knn_seed_skip_no_consumer`` logs at INFO or DEBUG
    depends on this process-wide set, so every test starts with it empty
    instead of inheriting what an earlier test reported."""
    monkeypatch.setattr(memory_service, "_KNN_SEED_NO_CONSUMER_REPORTED", set())


def _make_memory(workspace_id=None, context_id=None, user_id="user1"):
    """Build a Memory mock with the fields _create_knn_seed_edges reads."""
    memory = MagicMock()
    memory.id = uuid4()
    memory.user_id = user_id
    memory.workspace_id = workspace_id or uuid4()
    memory.context_id = context_id or uuid4()
    # #1403: real (non-mock) starting value for the server-only supersede
    # suggestion column the storage path assigns to.
    memory.supersede_candidate = None
    # #2016: the judge's pair fingerprint reads these as text.
    memory.summary = "new summary"
    memory.content = "new content"
    memory.created_at = datetime(2026, 10, 2)
    return memory


def _make_config(
    enabled=True,
    k=5,
    min_similarity=0.4,
    weight=0.3,
    suggest=True,
    verify_mode="off",
):
    """Build a NeuralMemoryConfig mock."""
    config = MagicMock()
    config.knn_seed_enabled = enabled
    config.supersede_suggest_enabled = suggest
    config.supersede_verify_mode = verify_mode
    config.supersede_verify_min_confidence = 0.7
    config.supersede_verify_max_calls_per_day = 200
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
    db.refresh = AsyncMock()

    # begin_nested() returns an async context manager
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=nested_cm)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=nested_cm)

    return db


def _make_edge_repo(existing_edges=None, created_edge=None, edge_to_candidate=None):
    """Build a NeuralEdgeRepository mock.

    Args:
        existing_edges: Return value for get_outgoing_edges (idempotency guard).
            Default [] = no existing edges = new memory, seeding proceeds.
            Non-empty = already seeded, seeding skipped.
        created_edge: Return value for create_edge_if_absent.
            Default MagicMock() simulates successful insert.
            None simulates ON CONFLICT DO NOTHING (edge already existed).
        edge_to_candidate: Return value for get_edge (the #1403 already-accepted
            check). Default None = no A->candidate edge yet, so a detected
            candidate is populated.
    """
    repo = MagicMock()
    repo.get_outgoing_edges = AsyncMock(return_value=existing_edges or [])
    repo.get_edge = AsyncMock(return_value=edge_to_candidate)
    repo.create_edge_if_absent = AsyncMock(
        return_value=created_edge if created_edge is not None else MagicMock()
    )
    return repo


class TestKnnSeeding:
    """Unit tests for _create_knn_seed_edges."""

    @pytest.mark.asyncio
    async def test_both_consumers_off_skips_search(self):
        """#2010: with seeding AND suggestions off, nothing reads the neighbour
        query, so no Qdrant search happens."""
        memory = _make_memory()
        db = _make_db()

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(enabled=False, suggest=False)),
            ),
            patch.object(memory_service, "_SUPERSEDE_SUGGEST_LAST_SEEN", False),
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
    async def test_supersede_candidate_detected_on_near_duplicate(self):
        """#1403: when the nearest neighbor is a near-duplicate (score >= the
        supersede-suggest threshold), a supersede_candidate_detected telemetry
        event names it — the signal a client can turn into a 'does this
        supersede X?' prompt (seeding is async, so the response can't carry it)."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        dup_id = str(uuid4())
        candidates = [
            {"id": dup_id, "score": 0.95, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.70, "payload": {}, "embedding": []},
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
            patch("services.memory_service.logger") as mock_logger,
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        events = {c.args[0]: c.kwargs for c in mock_logger.info.call_args_list if c.args}
        assert "supersede_candidate_detected" in events
        assert events["supersede_candidate_detected"]["candidate_memory_id"] == dup_id
        assert events["supersede_candidate_detected"]["similarity"] == 0.95

        # #1403 option B: the candidate is PERSISTED onto the server-only
        # supersede_candidate column so recall()/reference() can surface it without
        # re-running k-NN on the read path, committed independently of edge creation.
        stored = memory.supersede_candidate
        assert stored is not None
        assert stored["memory_id"] == dup_id
        assert stored["similarity"] == 0.95
        assert "detected_at" in stored
        db.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_supersede_candidate_detected_even_with_existing_declared_edge(self):
        """#1403 review F2: a memory created with linked_memory_ids/
        linked_source_uris/supersedes gets a declared edge synchronously (via
        _create_declared_links) BEFORE this async task runs, so the seed
        idempotency guard (existing_edges) fires. Detection must still run — it
        only reads candidates and writes the server-only supersede_candidate
        column — while SEEDING stays skipped. Regression: detection used to sit
        behind the guard and was silently skipped for exactly this workflow (the
        one most likely to be a supersede)."""
        memory = _make_memory()
        db = _make_db()
        # Non-empty outgoing edges: a declared link already exists (the F2 trigger).
        mock_repo = _make_edge_repo(existing_edges=[MagicMock()])
        dup_id = str(uuid4())
        candidates = [
            {"id": dup_id, "score": 0.95, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.70, "payload": {}, "embedding": []},
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

        # Detection still fires and persists despite the pre-existing edge...
        stored = memory.supersede_candidate
        assert stored is not None
        assert stored["memory_id"] == dup_id
        # ...but SEEDING is still skipped by the idempotency guard.
        mock_repo.create_edge_if_absent.assert_not_called()

    @pytest.mark.asyncio
    async def test_supersede_candidate_not_repopulated_when_already_accepted(self):
        """#1403 F2 regression guard: once a suggestion is accepted (a
        'supersedes' edge from the memory to the candidate exists and the column
        was cleared), a later re-embed must NOT re-populate the candidate — that
        would resurface an already-actioned suggestion on recall. Because
        detection now runs independent of the seed guard, this needs its own
        already-superseded check."""
        from models.memory import EDGE_TYPE_SUPERSEDES

        memory = _make_memory()
        db = _make_db()
        dup_id = str(uuid4())
        # An existing 'supersedes' edge memory -> dup: the suggestion was already
        # accepted and cleared. existing_edges non-empty so seeding is skipped too.
        accepted_edge = MagicMock()
        accepted_edge.edge_type = EDGE_TYPE_SUPERSEDES
        mock_repo = _make_edge_repo(existing_edges=[MagicMock()], edge_to_candidate=accepted_edge)
        candidates = [
            {"id": dup_id, "score": 0.95, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.70, "payload": {}, "embedding": []},
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

        # The already-accepted check queried the A->candidate edge...
        mock_repo.get_edge.assert_awaited_once()
        # ...found a 'supersedes' edge, so the candidate is NOT re-populated.
        assert memory.supersede_candidate is None
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_supersede_candidate_below_threshold(self):
        """#1403: a merely-related nearest neighbor (score below the supersede-
        suggest threshold 0.85) must NOT emit a supersede candidate nor store one
        — the signal fires only on true near-duplicates, not on ordinary seed
        neighbors."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        candidates = [
            {"id": str(uuid4()), "score": 0.80, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.70, "payload": {}, "embedding": []},
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
            patch("services.memory_service.logger") as mock_logger,
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        emitted = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
        assert "supersede_candidate_detected" not in emitted
        # No candidate stored either — the column stays None.
        assert memory.supersede_candidate is None

    @pytest.mark.asyncio
    async def test_supersede_candidate_independent_of_seed_threshold(self):
        """#1403 (PR #1415 review): supersede detection uses the RAW top-1 neighbor,
        not the seed-threshold-filtered list. A resolved/operator seed threshold
        ABOVE the supersede threshold (0.90 > 0.85) must NOT suppress a valid
        suggestion — and the below-seed-threshold neighbor still gets no seed edge."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        dup_id = str(uuid4())
        # 0.87 is a valid supersede candidate (>= 0.85) but BELOW the resolved seed
        # threshold 0.90 → filtered out of seed_neighbors (no edge), yet must still
        # be detected + persisted from the raw k-NN results.
        candidates = [
            {"id": dup_id, "score": 0.87, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.70, "payload": {}, "embedding": []},
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=0.90),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch("services.memory_service.logger") as mock_logger,
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        events = {c.args[0]: c.kwargs for c in mock_logger.info.call_args_list if c.args}
        assert "supersede_candidate_detected" in events
        assert events["supersede_candidate_detected"]["candidate_memory_id"] == dup_id
        assert memory.supersede_candidate is not None
        assert memory.supersede_candidate["memory_id"] == dup_id
        # 0.87 < 0.90 resolved seed threshold → no seed edge created.
        mock_repo.create_edge_if_absent.assert_not_awaited()

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
        # Issue #741: edge_type='semantic_similarity' deprecated; discriminator
        # moved to origin='semantic'. The row writes neural_association with
        # the origin kwarg carrying the semantic signal.
        from models.memory import EDGE_ORIGIN_SEMANTIC, EDGE_TYPE_NEURAL_ASSOCIATION

        assert call.kwargs["edge_type"] == EDGE_TYPE_NEURAL_ASSOCIATION
        assert call.kwargs["origin"] == EDGE_ORIGIN_SEMANTIC
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

        # Top score < 0.85 supersede threshold: keeps this test isolated to edge
        # logic (no extra candidate-storage commit, #1403).
        candidates = [
            {"id": str(uuid4()), "score": 0.84, "payload": {}, "embedding": []},
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
    async def test_missing_context_skips(self):
        """A memory without a context_id cannot be placed, so seeding is skipped.
        (workspace_id is NOT NULL since e95 / #1951; only the context half of the
        old guard remains.)"""
        memory = _make_memory()
        memory.context_id = None
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

        # Top score < 0.85 supersede threshold: no candidate-storage commit, so
        # this test still asserts "no commit when nothing inserted" (#1403).
        candidates = [
            {"id": str(uuid4()), "score": 0.84, "payload": {}, "embedding": []},
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
        """Idempotency guard: if memory already has outgoing edges, skip SEEDING.

        This prevents update_memory() re-embeds from overwriting existing
        Hebbian-learned edges via ON CONFLICT DO UPDATE on the
        (user_id, src_id, dst_id) unique constraint.

        Note (#1403 F2): the Qdrant search now runs BEFORE this guard because
        supersede detection depends on it, so ``search`` IS called — but with an
        only-related (below-threshold) neighbor there is no supersede commit, and
        the guard still skips edge creation.
        """
        memory = _make_memory()
        db = _make_db()

        # Simulate an existing edge (e.g., from prior Hebbian learning)
        existing_edge = MagicMock()
        mock_repo = _make_edge_repo(existing_edges=[existing_edge])
        # A merely-related neighbor (below the 0.85 supersede threshold) so no
        # supersede_candidate is detected/committed on this path.
        candidates = [{"id": str(uuid4()), "score": 0.5, "payload": {}, "embedding": []}]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config()),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
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

        # Guard: get_outgoing_edges returned existing edges, so edge SEEDING is
        # skipped. The search runs (supersede detection needs it) but finds no
        # near-duplicate, so nothing is committed.
        mock_repo.get_outgoing_edges.assert_called_once()
        mock_search.assert_called_once()
        mock_repo.create_edge_if_absent.assert_not_called()
        assert memory.supersede_candidate is None
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_threshold_skips_seeding_and_triggers_bootstrap(self):
        """D4 step 3: ``resolve_knn_threshold`` → None disables SEEDING
        for this call and invokes ``maybe_trigger_bootstrap`` (best-effort).

        Guards the Phase B fallback-chain wiring in ``_create_knn_seed_edges``
        so a regression that silently skips the bootstrap trigger (or creates
        seed edges without a calibrated threshold) is caught early.
        (Copilot review PR #420 loop 6.) Since #2006 the candidates search
        still runs — the supersede-suggestion detection reads it.
        """
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        candidates = [
            {"id": str(uuid4()), "score": 0.70, "payload": {}, "embedding": []},
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(min_similarity=None)),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=None),
            ) as mock_resolve,
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ) as mock_search,
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "tasks.neural_calibration.maybe_trigger_bootstrap",
                new=AsyncMock(return_value=True),
            ) as mock_bootstrap,
            patch.object(memory_service, "_KNN_SEED_UNCALIBRATED_WARNED", set()),
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
        # The shared candidates query still runs (supersede detection, #2006),
        # sized for the top non-self neighbour only.
        mock_search.assert_awaited_once()
        assert mock_search.await_args.kwargs["limit"] == 2
        # No seed edges without a calibrated threshold.
        mock_repo.create_edge_if_absent.assert_not_called()
        # Below the supersede threshold: nothing persisted.
        assert memory.supersede_candidate is None
        db.commit.assert_not_called()
        # Bootstrap trigger WAS invoked — subsequent remember() calls can
        # find a populated calibration row once D3 is crossed.
        mock_bootstrap.assert_awaited_once()
        # dimensions argument plumbed correctly (len(vector) = 512)
        call_kwargs = mock_bootstrap.await_args.kwargs
        assert call_kwargs["model_name"] == "text-embedding-3-small"
        assert call_kwargs["dimensions"] == 512

    @pytest.mark.asyncio
    async def test_none_threshold_still_detects_supersede_candidate(self):
        """#2006: an embedder with no ``knn_seed`` calibration row must still
        get supersede suggestions — detection is independent of the seed
        threshold. Only the seed edges are skipped."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        dup_id = str(uuid4())
        candidates = [
            {"id": str(memory.id), "score": 1.0, "payload": {}, "embedding": []},
            {"id": dup_id, "score": 0.93, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.70, "payload": {}, "embedding": []},
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(min_similarity=None)),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ) as mock_search,
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "tasks.neural_calibration.maybe_trigger_bootstrap",
                new=AsyncMock(return_value=True),
            ) as mock_bootstrap,
            patch.object(memory_service, "_KNN_SEED_UNCALIBRATED_WARNED", set()),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        mock_search.assert_awaited_once()
        assert mock_search.await_args.kwargs["limit"] == 2
        stored = memory.supersede_candidate
        assert stored is not None
        assert stored["memory_id"] == dup_id
        assert stored["similarity"] == 0.93
        db.commit.assert_awaited_once()
        mock_repo.create_edge_if_absent.assert_not_called()
        mock_bootstrap.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_none_threshold_warns_once_per_model_and_dims_past_d3(self):
        """#2006: once the D3 bootstrap gate is crossed (maybe_trigger_bootstrap
        returned True) a missing calibration row means seed edges are really
        off, so it is logged at WARNING — once per process per (model, dims).
        Below D3 (bootstrap returned False) it is the expected state of a fresh
        deployment and stays at DEBUG. The debug skip line fires every call."""
        db = _make_db()
        mock_repo = _make_edge_repo()

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(min_similarity=None)),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "tasks.neural_calibration.maybe_trigger_bootstrap",
                new=AsyncMock(side_effect=[False, True, True, True]),
            ),
            patch.object(memory_service, "_KNN_SEED_UNCALIBRATED_WARNED", set()),
            patch("services.memory_service.logger") as mock_logger,
        ):
            for model, dims in (
                ("model-a", 512),  # below D3 → no warning
                ("model-a", 512),  # D3 crossed → warning
                ("model-a", 512),  # already warned
                ("model-a", 768),  # new pair → warning
            ):
                await _create_knn_seed_edges(
                    db=db,
                    memory=_make_memory(),
                    vector=[0.1] * dims,
                    collection_name="kagura_memories",
                    model_name=model,
                )

        warnings = [
            c.kwargs
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "knn_seed_uncalibrated_embedder"
        ]
        assert [(w["model"], w["dimensions"]) for w in warnings] == [
            ("model-a", 512),
            ("model-a", 768),
        ]
        assert all("supersede detection still runs" in w["hint"] for w in warnings)
        debugs = [
            c
            for c in mock_logger.debug.call_args_list
            if c.args and c.args[0] == "knn_seed_skip_no_threshold"
        ]
        assert len(debugs) == 4

    @pytest.mark.asyncio
    async def test_none_threshold_bootstrap_failure_rolls_back_then_detects(self):
        """#2006 review: a DB error swallowed inside the bootstrap trigger would
        leave the session aborted for the supersede detection that now follows.
        The session is rolled back, and detection still persists the candidate."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        dup_id = str(uuid4())
        candidates = [
            {"id": str(memory.id), "score": 1.0, "payload": {}, "embedding": []},
            {"id": dup_id, "score": 0.91, "payload": {}, "embedding": []},
        ]

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(min_similarity=None)),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch(
                "tasks.neural_calibration.maybe_trigger_bootstrap",
                new=AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch.object(memory_service, "_KNN_SEED_UNCALIBRATED_WARNED", set()),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        db.rollback.assert_awaited_once()
        stored = memory.supersede_candidate
        assert stored is not None
        assert stored["memory_id"] == dup_id
        db.commit.assert_awaited_once()
        mock_repo.create_edge_if_absent.assert_not_called()


class TestSupersedeSuggestFlag:
    """#2010: knn_seed_enabled gates only seed edges; supersede_suggest_enabled
    gates only the #1403 detection."""

    @staticmethod
    def _candidates(memory, dup_id):
        return [
            {"id": str(memory.id), "score": 1.0, "payload": {}, "embedding": []},
            {"id": dup_id, "score": 0.93, "payload": {}, "embedding": []},
            {"id": str(uuid4()), "score": 0.70, "payload": {}, "embedding": []},
        ]

    @pytest.mark.asyncio
    async def test_seed_off_still_detects_candidate_without_edges(self):
        """knn_seed_enabled=False must not silence suggestions: the neighbour
        query runs (2 hits suffice), the candidate is stored, no seed edge is
        written and the seed threshold is never resolved."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        dup_id = str(uuid4())

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(enabled=False)),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=0.5),
            ) as mock_threshold,
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=self._candidates(memory, dup_id)),
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

        mock_search.assert_awaited_once()
        assert mock_search.await_args.kwargs["limit"] == 2
        stored = memory.supersede_candidate
        assert stored is not None
        assert stored["memory_id"] == dup_id
        mock_repo.create_edge_if_absent.assert_not_called()
        mock_repo.get_outgoing_edges.assert_not_called()
        mock_threshold.assert_not_called()
        mock_bootstrap.assert_not_called()

    @pytest.mark.asyncio
    async def test_suggest_off_seeds_edges_without_candidate(self):
        """supersede_suggest_enabled=False: seed edges are created as before,
        but no supersede_candidate is stored, and one INFO line says so."""
        memory = _make_memory()
        db = _make_db()
        mock_repo = _make_edge_repo()
        dup_id = str(uuid4())

        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(min_similarity=0.6, suggest=False)),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=0.6),
            ),
            patch(
                "db.qdrant.search_memories_qdrant",
                new=AsyncMock(return_value=self._candidates(memory, dup_id)),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=mock_repo,
            ),
            patch.object(memory_service, "_SUPERSEDE_SUGGEST_LAST_SEEN", None),
            patch("services.memory_service.logger") as mock_logger,
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )
            assert memory_service._SUPERSEDE_SUGGEST_LAST_SEEN is False

        assert memory.supersede_candidate is None
        mock_repo.get_edge.assert_not_called()
        # Both non-self neighbours clear the 0.6 seed threshold → two seed edges.
        dst_ids = {str(c.kwargs["dst_id"]) for c in mock_repo.create_edge_if_absent.call_args_list}
        assert dup_id in dst_ids
        assert str(memory.id) not in dst_ids
        assert mock_repo.create_edge_if_absent.await_count == 2
        events = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
        assert events.count("supersede_suggest_disabled") == 1
        assert "supersede_candidate_detected" not in events

    @pytest.mark.asyncio
    async def test_suggest_disabled_logged_per_transition_not_per_ingest(self):
        """The disabled notice is one line per off-transition: three ingests
        while off log once; on, then off again, logs a second time."""
        db = _make_db()
        configs = [_make_config(enabled=False, suggest=False)] * 3 + [
            _make_config(enabled=False, suggest=True),
            _make_config(enabled=False, suggest=False),
        ]
        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(side_effect=configs),
            ),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock()) as mock_search,
            patch.object(memory_service, "_SUPERSEDE_SUGGEST_LAST_SEEN", None),
            patch("services.memory_service.logger") as mock_logger,
        ):
            for _ in configs:
                await _create_knn_seed_edges(
                    db=db,
                    memory=_make_memory(),
                    vector=[0.1] * 512,
                    collection_name="kagura_memories",
                    model_name="text-embedding-3-small",
                )

        # The one suggest-on ingest ran the query (seeding off → limit 2, no hits).
        assert mock_search.await_count == 1
        events = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
        assert events.count("supersede_suggest_disabled") == 2

    @pytest.mark.asyncio
    async def test_seed_on_uncalibrated_suggest_off_skips_search(self):
        """Seeding on but no calibration row (threshold None) and suggestions
        off: nothing would read the neighbour query, so it is skipped and the
        bootstrap trigger still fires (#2010 review)."""
        memory = _make_memory()
        db = _make_db()
        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(min_similarity=None, suggest=False)),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=None),
            ),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock()) as mock_search,
            patch(
                "tasks.neural_calibration.maybe_trigger_bootstrap",
                new=AsyncMock(return_value=False),
            ) as mock_bootstrap,
            patch.object(memory_service, "_SUPERSEDE_SUGGEST_LAST_SEEN", False),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        mock_bootstrap.assert_awaited_once()
        mock_search.assert_not_called()
        assert memory.supersede_candidate is None

    @pytest.mark.asyncio
    async def test_skip_no_consumer_logged_at_info_once_per_embedder(self):
        """#2023: the no-consumer skip (seeding on but uncalibrated, suggestions
        off) logs INFO on the first ingest per (model, dims) in a process, so
        the state is visible at LOG_LEVEL=info, and DEBUG after that."""
        with (
            patch(
                "neural.config.NeuralMemoryConfig.from_db",
                new=AsyncMock(return_value=_make_config(min_similarity=None, suggest=False)),
            ),
            patch(
                "neural.calibration.resolve_knn_threshold",
                new=AsyncMock(return_value=None),
            ),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock()) as mock_search,
            patch(
                "tasks.neural_calibration.maybe_trigger_bootstrap",
                new=AsyncMock(return_value=False),
            ),
            patch.object(memory_service, "_SUPERSEDE_SUGGEST_LAST_SEEN", False),
            patch.object(memory_service, "_KNN_SEED_NO_CONSUMER_REPORTED", set()),
            patch("services.memory_service.logger") as mock_logger,
        ):
            for model, dims in (
                ("model-a", 512),  # first → INFO
                ("model-a", 512),  # already reported → DEBUG
                ("model-a", 768),  # new pair → INFO
                ("model-a", 768),  # DEBUG
            ):
                await _create_knn_seed_edges(
                    db=_make_db(),
                    memory=_make_memory(),
                    vector=[0.1] * dims,
                    collection_name="kagura_memories",
                    model_name=model,
                )

        mock_search.assert_not_called()
        infos = [
            c.kwargs
            for c in mock_logger.info.call_args_list
            if c.args and c.args[0] == "knn_seed_skip_no_consumer"
        ]
        assert [(i["model"], i["dimensions"]) for i in infos] == [
            ("model-a", 512),
            ("model-a", 768),
        ]
        assert all("SUPERSEDE_SUGGEST_ENABLED" in i["hint"] for i in infos)
        debugs = [
            c.kwargs
            for c in mock_logger.debug.call_args_list
            if c.args and c.args[0] == "knn_seed_skip_no_consumer"
        ]
        assert [(d["model"], d["dimensions"]) for d in debugs] == [
            ("model-a", 512),
            ("model-a", 768),
        ]


class TestSupersedeSuggestConfig:
    """#2010: the flag is env-only — default on, SUPERSEDE_SUGGEST_ENABLED=false
    turns it off, and a neural_config row never reaches it."""

    def test_default_on(self):
        from neural.config import NeuralMemoryConfig

        assert NeuralMemoryConfig().supersede_suggest_enabled is True

    def test_env_turns_it_off(self, monkeypatch):
        from neural.config import NeuralMemoryConfig

        monkeypatch.setenv("SUPERSEDE_SUGGEST_ENABLED", "false")
        assert NeuralMemoryConfig.from_env().supersede_suggest_enabled is False

    @pytest.mark.asyncio
    async def test_db_row_does_not_override(self, monkeypatch):
        from neural.config import NeuralMemoryConfig

        monkeypatch.delenv("SUPERSEDE_SUGGEST_ENABLED", raising=False)
        NeuralMemoryConfig.invalidate_cache()

        class _Row:
            key = "supersede_suggest_enabled"

            @staticmethod
            def get_typed_value():
                return "false"

        result = MagicMock()
        result.scalars.return_value.all.return_value = [_Row()]
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        try:
            config = await NeuralMemoryConfig.from_db(db)
        finally:
            NeuralMemoryConfig.invalidate_cache()
        assert config.supersede_suggest_enabled is True


class TestSupersedeVerification:
    """#2016: with supersede_verify_mode="llm" the pair is judged before the
    candidate is stored; the verdict rides in the column."""

    @staticmethod
    def _run(memory, db, config, judge, *, stored=None):
        dup_id = str(uuid4())
        candidates = [
            {"id": str(memory.id), "score": 1.0, "payload": {}, "embedding": []},
            {"id": dup_id, "score": 0.93, "payload": {}, "embedding": []},
        ]
        old = MagicMock()
        old.id = dup_id
        old.summary, old.content = "old summary", "old content"
        old.created_at = datetime(2026, 10, 1)
        row = MagicMock()
        row.scalar_one_or_none = MagicMock(return_value=old)
        db.execute = AsyncMock(return_value=row)
        memory.supersede_candidate = stored
        return dup_id, candidates, judge

    @pytest.mark.asyncio
    async def test_llm_mode_stores_the_verdict(self):
        memory = _make_memory()
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="llm")
        verdict = {"verdict": "supersedes", "confidence": 0.92, "verified": True}
        judge = AsyncMock(return_value=verdict)
        dup_id, candidates, _ = self._run(memory, db, config, judge)

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=_make_edge_repo()),
            patch("services.supersede_verify.judge_pair", new=judge),
            patch("services.memory_service.logger") as mock_logger,
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        judge.assert_awaited_once()
        assert judge.await_args.kwargs["new"] is memory
        assert str(judge.await_args.kwargs["old"].id) == dup_id
        stored = memory.supersede_candidate
        assert stored["memory_id"] == dup_id
        assert stored["verification"] == verdict
        events = {c.args[0]: c.kwargs for c in mock_logger.info.call_args_list if c.args}
        assert events["supersede_candidate_detected"]["verdict"] == "supersedes"

    @pytest.mark.asyncio
    async def test_parallel_verdict_is_stored_too(self):
        """A parallel verdict is kept in the column (the resolver hides it and
        a re-embed will not re-judge the pair)."""
        memory = _make_memory()
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="llm")
        verdict = {"verdict": "parallel", "confidence": 0.8, "verified": False}
        judge = AsyncMock(return_value=verdict)
        dup_id, candidates, _ = self._run(memory, db, config, judge)

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=_make_edge_repo()),
            patch("services.supersede_verify.judge_pair", new=judge),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        assert memory.supersede_candidate["verification"]["verdict"] == "parallel"
        assert memory.supersede_candidate["memory_id"] == dup_id

    @pytest.mark.asyncio
    async def test_judge_none_stores_unverified_candidate(self):
        """A judge that answers None (off, capped, failed) leaves the candidate
        as before #2016: no verification key at all."""
        memory = _make_memory()
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="llm")
        judge = AsyncMock(return_value=None)
        dup_id, candidates, _ = self._run(memory, db, config, judge)

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=_make_edge_repo()),
            patch("services.supersede_verify.judge_pair", new=judge),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        assert memory.supersede_candidate == {
            "memory_id": dup_id,
            "similarity": 0.93,
            "detected_at": memory.supersede_candidate["detected_at"],
        }

    @pytest.mark.asyncio
    async def test_off_mode_never_calls_the_judge_or_loads_the_target(self):
        memory = _make_memory()
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="off")
        judge = AsyncMock()
        dup_id, candidates, _ = self._run(memory, db, config, judge)

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=_make_edge_repo()),
            patch("services.supersede_verify.judge_pair", new=judge),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        judge.assert_not_called()
        db.execute.assert_not_called()
        assert "verification" not in memory.supersede_candidate

    @pytest.mark.asyncio
    async def test_unmoved_pair_reuses_the_stored_verdict(self):
        """A mechanical re-embed (same target, same texts, similarity within
        the resurface delta) does not spend a judge call: the stored verdict
        is kept."""
        from services.supersede_verify import pair_fingerprint

        memory = _make_memory()
        memory.summary, memory.content = "new summary", "new content"
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="llm")
        judge = AsyncMock()
        dup_id, candidates, _ = self._run(memory, db, config, judge)
        old = db.execute.return_value.scalar_one_or_none.return_value
        previous = {
            "verdict": "parallel",
            "confidence": 0.8,
            "verified": False,
            "fingerprint": pair_fingerprint(old, memory),
        }
        memory.supersede_candidate = {
            "memory_id": dup_id,
            "similarity": 0.925,
            "detected_at": "2026-10-01T00:00:00Z",
            "verification": previous,
        }

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=_make_edge_repo()),
            patch("services.supersede_verify.judge_pair", new=judge),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        judge.assert_not_called()
        assert memory.supersede_candidate["verification"] == previous
        assert memory.supersede_candidate["similarity"] == 0.93

    @pytest.mark.asyncio
    async def test_edited_text_is_judged_again(self):
        """The same pair with an edited summary (fingerprint moved) is re-judged
        even though the similarity barely moved — cosine cannot see the edit
        that flips the verdict."""
        memory = _make_memory()
        memory.summary, memory.content = "Depot A opens 9:00", "c"
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="llm")
        fresh = {"verdict": "supersedes", "confidence": 0.9, "verified": True}
        judge = AsyncMock(return_value=fresh)
        dup_id, candidates, _ = self._run(memory, db, config, judge)
        memory.supersede_candidate = {
            "memory_id": dup_id,
            "similarity": 0.925,
            "detected_at": "2026-10-01T00:00:00Z",
            "verification": {"verdict": "parallel", "fingerprint": "stale-texts"},
        }

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=_make_edge_repo()),
            patch("services.supersede_verify.judge_pair", new=judge),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        judge.assert_awaited_once()
        assert memory.supersede_candidate["verification"] == fresh

    @pytest.mark.asyncio
    async def test_dismissal_during_the_judge_call_wins(self):
        """A tombstone committed while the judge was out (re-read after the
        call) suppresses the write; the verdict is not stored."""
        memory = _make_memory()
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="llm")
        dup_id, candidates, _ = self._run(memory, db, config, None)

        async def judge(*_a, **_k):
            # Simulates update_memory(dismiss_supersede_candidate=true) landing
            # mid-call; db.refresh would reload this from the row.
            memory.supersede_candidate = {
                "dismissed": {"memory_id": dup_id, "similarity": 0.93, "dismissed_at": "x"}
            }
            return {"verdict": "supersedes", "confidence": 0.9, "verified": True}

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=_make_edge_repo()),
            patch("services.supersede_verify.judge_pair", new=judge),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        db.refresh.assert_awaited_once()
        assert "dismissed" in memory.supersede_candidate
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_accept_during_the_judge_call_wins(self):
        """A supersedes edge created while the judge was out (create_edge
        accepted the suggestion and cleared the column) suppresses the write."""
        from models.memory import EDGE_TYPE_SUPERSEDES

        memory = _make_memory()
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="llm")
        dup_id, candidates, _ = self._run(memory, db, config, None)
        repo = _make_edge_repo()
        accepted = MagicMock()
        accepted.edge_type = EDGE_TYPE_SUPERSEDES
        # First get_edge (before the judge): nothing; second (after): accepted.
        repo.get_edge = AsyncMock(side_effect=[None, accepted])

        async def judge(*_a, **_k):
            memory.supersede_candidate = None  # cleared by the accept
            return {"verdict": "supersedes", "confidence": 0.9, "verified": True}

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=repo),
            patch("services.supersede_verify.judge_pair", new=judge),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        assert repo.get_edge.await_count == 2
        assert memory.supersede_candidate is None
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_neighbour_written_later_is_not_judged(self):
        """The top-1 neighbour was created AFTER this memory (its embedding
        finished first): asking whether the earlier text supersedes the later
        one inverts the question, so the candidate stays unverified."""
        memory = _make_memory()
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="llm")
        judge = AsyncMock()
        dup_id, candidates, _ = self._run(memory, db, config, judge)
        db.execute.return_value.scalar_one_or_none.return_value.created_at = datetime(2026, 10, 3)

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=_make_edge_repo()),
            patch("services.supersede_verify.judge_pair", new=judge),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        judge.assert_not_called()
        assert memory.supersede_candidate["memory_id"] == dup_id
        assert "verification" not in memory.supersede_candidate

    @pytest.mark.asyncio
    async def test_llm_mode_with_a_tombstone_never_calls_the_judge(self):
        """The #1504 dismissal guard runs before the judge: a rejected pair
        costs no LLM call."""
        memory = _make_memory()
        db = _make_db()
        config = _make_config(enabled=False, verify_mode="llm")
        judge = AsyncMock()
        dup_id, candidates, _ = self._run(memory, db, config, judge)
        memory.supersede_candidate = {
            "dismissed": {"memory_id": dup_id, "similarity": 0.93, "dismissed_at": "x"}
        }

        with (
            patch("neural.config.NeuralMemoryConfig.from_db", new=AsyncMock(return_value=config)),
            patch("db.qdrant.search_memories_qdrant", new=AsyncMock(return_value=candidates)),
            patch("repositories.neural_edge.NeuralEdgeRepository", return_value=_make_edge_repo()),
            patch("services.supersede_verify.judge_pair", new=judge),
        ):
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=[0.1] * 512,
                collection_name="kagura_memories",
                model_name="text-embedding-3-small",
            )

        judge.assert_not_called()
        db.execute.assert_not_called()
        assert "dismissed" in memory.supersede_candidate
