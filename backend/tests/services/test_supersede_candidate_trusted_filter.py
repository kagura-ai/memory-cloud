"""Supersede suggestions honour the trusted filter (#2013 review).

Under ``filters={"trust_tier": "trusted"}`` recall drops connector-sourced
rows (#887), but ``supersede_candidate`` names another memory and carries its
summary. A connector-sourced target in a trusted context would reach the reply
through the suggestion. get_agent_bootstrap always recalls under the trusted
filter and, since #2013, serves recall's full rows — so the suggestion is a
way into the bootstrap reply too. The resolver drops such a target when the
recall is trusted-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.memory import SOURCE_TYPE_CONNECTOR, SOURCE_TYPE_MANUAL
from models.schemas import RecallRequest, RecallResponse
from services.agent_bootstrap_service import (
    AgentBootstrapService,
    BootstrapParams,
    BootstrapPrincipal,
)
from services.memory_service import MemoryService
from utils.recall_projection import recall_result_item

WS = uuid.uuid4()
CTX = uuid.uuid4()


def _source(target_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        summary="the newer fact",
        context_summary=None,
        type="note",
        importance=0.5,
        scope="working",
        created_at=datetime(2026, 10, 1),
        updated_at=None,
        last_used_at=None,
        access_count=0,
        accessed_by_clients=None,
        confidence=1.0,
        client="test",
        tags=[],
        context=None,
        source_uri=None,
        source_type=SOURCE_TYPE_MANUAL,
        workspace_id=WS,
        context_id=CTX,
        supersede_candidate={
            "memory_id": str(target_id),
            "similarity": 0.93,
            "detected_at": "2026-10-01T00:00:00Z",
        },
    )


def _target(target_id: uuid.UUID, source_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=target_id,
        summary="text a connector ingested",
        workspace_id=WS,
        context_id=CTX,
        type="note",
        source_type=source_type,
        content_head="body",
    )


def _service(target: SimpleNamespace) -> MemoryService:
    service = MemoryService(MagicMock())
    result = MagicMock()
    result.all = MagicMock(return_value=[target])
    service.db.execute = AsyncMock(return_value=result)
    return service


async def _page(request: RecallRequest, source_type: str):
    target_id = uuid.uuid4()
    source = _source(target_id)
    service = _service(_target(target_id, source_type))
    return await service._recall_build_responses(
        request,
        [{"id": "p1", "score": 0.9}],
        {"p1": source},
        shadow_map={},
        contradiction_map={},
        user_id="u",
    )


TRUSTED = {"trust_tier": "trusted"}


@pytest.mark.asyncio
class TestResolver:
    async def test_trusted_only_drops_a_connector_sourced_target(self):
        target_id = uuid.uuid4()
        source = _source(target_id)
        service = _service(_target(target_id, SOURCE_TYPE_CONNECTOR))

        resolved = await service._resolve_supersede_candidates(
            {source.id: source}, user_id="u", operation="recall", trusted_only=True
        )

        assert resolved == {}

    async def test_trusted_only_keeps_a_manual_target(self):
        target_id = uuid.uuid4()
        source = _source(target_id)
        service = _service(_target(target_id, SOURCE_TYPE_MANUAL))

        resolved = await service._resolve_supersede_candidates(
            {source.id: source}, user_id="u", operation="recall", trusted_only=True
        )

        assert resolved[source.id].memory_id == target_id

    async def test_an_unfiltered_read_keeps_a_connector_target(self):
        target_id = uuid.uuid4()
        source = _source(target_id)
        service = _service(_target(target_id, SOURCE_TYPE_CONNECTOR))

        resolved = await service._resolve_supersede_candidates(
            {source.id: source}, user_id="u", operation="recall"
        )

        assert resolved[source.id].memory_id == target_id


@pytest.mark.asyncio
class TestRecallPage:
    async def test_a_trusted_recall_carries_no_connector_suggestion(self):
        page = await _page(RecallRequest(query="q", k=5, filters=TRUSTED), SOURCE_TYPE_CONNECTOR)

        assert page[0].supersede_candidate is None
        assert "supersede_candidate" not in recall_result_item(page[0], "full")

    async def test_an_unfiltered_recall_still_carries_it(self):
        page = await _page(RecallRequest(query="q", k=5), SOURCE_TYPE_CONNECTOR)

        assert page[0].supersede_candidate is not None


@pytest.mark.asyncio
class TestBootstrapRecall:
    async def test_the_bootstrap_recall_request_drops_a_connector_suggestion(self):
        # The request bootstrap hands MemoryService.recall, fed to the page
        # builder that recall runs: no connector summary reaches the row.
        recall = AsyncMock(return_value=RecallResponse(results=[]))
        with patch("services.memory_service.MemoryService") as ms_cls:
            ms_cls.return_value.recall = recall
            await AgentBootstrapService(MagicMock())._recall(
                SimpleNamespace(id=CTX, workspace_id=WS),
                BootstrapParams(agent_id=uuid.uuid4(), query="q"),
                BootstrapPrincipal(user_id="u", workspace_id=WS, principal_type="agent"),
            )
        request = recall.await_args.args[0]

        page = await _page(request, SOURCE_TYPE_CONNECTOR)

        assert page[0].supersede_candidate is None
        assert "supersede_candidate" not in recall_result_item(page[0], "full")


class TestIsTrustedRecall:
    """One predicate for the trusted filter — the row filter, the supersede
    resolver and the selection-evidence guard read it the same way."""

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            (None, False),
            ({}, False),
            ({"trust_tier": "all"}, False),
            ({"source_type": "manual"}, False),
            ({"trust_tier": "trusted"}, True),
            ({"trust_tier": "trusted", "type": "note"}, True),
        ],
    )
    def test_reads_the_trust_tier_filter(self, filters, expected):
        from services.memory_service import _is_trusted_recall

        assert _is_trusted_recall(RecallRequest(query="q", filters=filters)) is expected
