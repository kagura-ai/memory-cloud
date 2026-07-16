"""MemoryService integration boundary for bootstrap selection evidence (#1306)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import RecallRequest
from services.memory_service import MemoryService
from services.recall_selection import RecallSelectionConfig
from utils.datetime import utcnow


def _memory(memory_id):
    return SimpleNamespace(
        id=memory_id,
        summary_embedding_id=None,
        summary=f"summary-{memory_id}",
        content=f"secret-content-{memory_id}",
        context_summary=None,
        type="knowledge",
        importance=0.5,
        scope="persistent",
        created_at=utcnow(),
        updated_at=None,
        client="api",
        tags=[],
        context=None,
        source_uri=None,
        source_type="manual",
        reference_count=0,
        last_used_at=None,
        access_count=0,
        confidence=1.0,
    )


@pytest.mark.asyncio
async def test_selection_evidence_requires_the_trusted_recall_boundary() -> None:
    service = MemoryService(MagicMock())

    with pytest.raises(ValueError, match="trusted-tier"):
        await service.recall(
            RecallRequest(query="q", k=2),
            user_id="operator",
            current_context_id=uuid4(),
            current_workspace_id=uuid4(),
            selection_config=RecallSelectionConfig(
                seed=188, exploration_floor=0.01, candidate_pool_k=100
            ),
        )


@pytest.mark.asyncio
async def test_empty_eligible_pool_returns_replay_stamp_without_claiming_positivity() -> None:
    context_id = uuid4()
    config = SimpleNamespace(
        reinforce_enabled=False,
        reinforce_require_host_arbitration=False,
    )
    service = MemoryService(MagicMock())
    service.search_service.hybrid_search = AsyncMock(return_value=[])

    with patch(
        "repositories.config_repository.ContextSearchConfigRepository.get_by_context",
        new=AsyncMock(return_value=config),
    ):
        response = await service.recall(
            RecallRequest(
                query="no candidate",
                k=2,
                filters={"trust_tier": "trusted"},
                search_mode="hybrid",
            ),
            user_id="operator",
            current_context_id=context_id,
            current_workspace_id=uuid4(),
            selection_config=RecallSelectionConfig(
                seed=188, exploration_floor=0.01, candidate_pool_k=100
            ),
        )

    assert response.selection_evidence is not None
    assert response.selection_evidence["selection_probabilities"] == {}
    assert response.selection_evidence["selection_policy"]["minimum_selection_probability"] is None


@pytest.mark.asyncio
async def test_selection_evidence_uses_post_trust_pool_and_never_serializes_content() -> None:
    context_id = uuid4()
    workspace_id = uuid4()
    trusted = [_memory(uuid4()) for _ in range(3)]
    filtered_id = uuid4()  # Search hit omitted by the authoritative PG/trust hydration.
    search_results = [
        {"id": str(memory.id), "score": 1.0 - index / 10, "hybrid_score": 1.0 - index / 10}
        for index, memory in enumerate(trusted)
    ] + [{"id": str(filtered_id), "score": 0.1, "hybrid_score": 0.1}]

    db = MagicMock()
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = trusted
    db.execute = AsyncMock(return_value=rows)
    db.commit = AsyncMock()
    service = MemoryService(db)
    service.search_service.hybrid_search = AsyncMock(return_value=search_results)
    service.memory_repo.update_access_stats = AsyncMock()
    service._check_and_promote = AsyncMock()
    service._apply_supersede_shadowing = AsyncMock(return_value=({}, {}))
    service._maybe_reinforce_rerank = AsyncMock()
    service._maybe_graph_boost = AsyncMock()

    search_config = SimpleNamespace(
        reinforce_enabled=False,
        reinforce_require_host_arbitration=False,
    )
    with (
        patch(
            "repositories.config_repository.ContextSearchConfigRepository.get_by_context",
            new=AsyncMock(return_value=search_config),
        ),
        patch(
            "services.agent_binding_service.filter_memory_rows_by_binding",
            new=AsyncMock(side_effect=lambda _db, rows, **_kwargs: (rows, 0)),
        ),
        patch(
            "services.memory_access_event_writer.emit_memory_access_event",
            new=AsyncMock(),
        ),
    ):
        response = await service.recall(
            RecallRequest(
                query="registered evaluation query",
                k=2,
                filters={"trust_tier": "trusted"},
                search_mode="hybrid",
            ),
            user_id="operator",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
            selection_config=RecallSelectionConfig(
                seed=188,
                exploration_floor=0.1,
                candidate_pool_k=100,
            ),
        )

    assert response.selection_evidence is not None
    probabilities = response.selection_evidence["selection_probabilities"]
    assert set(probabilities) == {str(memory.id) for memory in trusted}
    assert str(filtered_id) not in probabilities
    assert all(probability > 0.0 for probability in probabilities.values())
    assert response.selection_evidence["selection_policy"]["ranking_policy"] == {
        "name": "production_hybrid_recall_v1",
        "search_mode": "hybrid",
        "use_rerank": False,
        "reinforce_enabled": False,
        "reinforce_require_host_arbitration": False,
        "graph_boost_enabled": False,
        "graph_boost_max": 0.15,
        "trust_filter": "trusted",
    }
    # The ordinary RecallResponse boundary cannot leak even the identity map;
    # bootstrap must explicitly copy it into its own additive component metadata.
    dumped = response.model_dump(mode="json")
    assert "selection_evidence" not in dumped
    assert "secret-content" not in repr(response.selection_evidence)


@pytest.mark.asyncio
async def test_registered_pool_is_bounded_at_candidate_pool_k() -> None:
    # The production over-fetch (neural k*4, cluster buffers) can exceed
    # candidate_pool_k; the stamped policy promises a bounded registered pool,
    # so the EVIDENCE must be truncated to the top candidate_pool_k eligible
    # candidates in production rank order (PR #1308 review). Production
    # results themselves are not truncated.
    context_id = uuid4()
    workspace_id = uuid4()
    trusted = [_memory(uuid4()) for _ in range(6)]
    search_results = [
        {"id": str(memory.id), "score": 1.0 - index / 10, "hybrid_score": 1.0 - index / 10}
        for index, memory in enumerate(trusted)
    ]

    db = MagicMock()
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = trusted
    db.execute = AsyncMock(return_value=rows)
    db.commit = AsyncMock()
    service = MemoryService(db)
    service.search_service.hybrid_search = AsyncMock(return_value=search_results)
    service.memory_repo.update_access_stats = AsyncMock()
    service._check_and_promote = AsyncMock()
    service._apply_supersede_shadowing = AsyncMock(return_value=({}, {}))
    service._maybe_reinforce_rerank = AsyncMock()
    service._maybe_graph_boost = AsyncMock()

    search_config = SimpleNamespace(
        reinforce_enabled=False,
        reinforce_require_host_arbitration=False,
    )
    with (
        patch(
            "repositories.config_repository.ContextSearchConfigRepository.get_by_context",
            new=AsyncMock(return_value=search_config),
        ),
        patch(
            "services.agent_binding_service.filter_memory_rows_by_binding",
            new=AsyncMock(side_effect=lambda _db, rows, **_kwargs: (rows, 0)),
        ),
        patch(
            "services.memory_access_event_writer.emit_memory_access_event",
            new=AsyncMock(),
        ),
    ):
        response = await service.recall(
            RecallRequest(
                query="bounded pool query",
                k=2,
                filters={"trust_tier": "trusted"},
                search_mode="hybrid",
            ),
            user_id="operator",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
            selection_config=RecallSelectionConfig(
                seed=188,
                exploration_floor=0.1,
                candidate_pool_k=4,
            ),
        )

    assert response.selection_evidence is not None
    probabilities = response.selection_evidence["selection_probabilities"]
    # Exactly the top candidate_pool_k eligible candidates, in rank order.
    assert set(probabilities) == {str(memory.id) for memory in trusted[:4]}
    assert response.selection_evidence["selection_policy"]["eligible_count"] == 4
    # Production results are unaffected by the evidence-side truncation.
    assert len(response.results) == 2
