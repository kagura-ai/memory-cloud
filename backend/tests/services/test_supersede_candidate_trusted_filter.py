"""Supersede suggestions honour the trusted filter (#2013 review, #2257).

Under ``filters={"trust_tier": "trusted"}`` recall drops connector-sourced
rows (#887), but ``supersede_candidate`` names another memory and carries its
summary. A connector-sourced target in a trusted context would reach the reply
through the suggestion. get_agent_bootstrap always recalls under the trusted
filter and, since #2013, serves recall's full rows — so the suggestion is a
way into the bootstrap reply too. Since #2257 the resolver holds the target to
the trusted filter inside its one target query
(``memory_listing.recall_trust_predicates``, read from the recall's
``filters``), so these cases assert the compiled statement; the behaviour on
real rows is pinned by
``tests/integration/test_2027_supersede_hint_row_filters_db.py`` (trust_tier).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from models.memory import SOURCE_TYPE_CONNECTOR, SOURCE_TYPE_MANUAL
from models.schemas import RecallRequest, RecallResponse
from services.agent_bootstrap_service import (
    AgentBootstrapService,
    BootstrapParams,
    BootstrapPrincipal,
)
from services.memory_service import MemoryService

WS = uuid.uuid4()
CTX = uuid.uuid4()

TRUST_CONTEXT = "contexts.trust_tier"
NOT_CONNECTOR = "memories.source_type !="


def _source(target_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        summary="the newer fact",
        context_summary=None,
        content="body",  # #2124
        type="note",
        importance=0.5,
        scope="working",
        created_at=datetime(2026, 10, 1),
        updated_at=None,
        version=1,  # #2043
        last_used_at=None,
        access_count=0,
        accessed_by_clients=None,
        confidence=1.0,
        client="test",
        tags=[],
        context=None,
        details=None,  # #2330
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


def _where(service: MemoryService) -> str:
    statement = service.db.execute.await_args.args[0]
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


async def _page_sql(request: RecallRequest) -> str:
    """The resolver's target query for a one-row page, as ``recall()`` runs it."""
    target_id = uuid.uuid4()
    source = _source(target_id)
    service = _service(_target(target_id, SOURCE_TYPE_CONNECTOR))
    await service._recall_build_responses(
        request,
        [{"id": "p1", "score": 0.9}],
        {"p1": source},
        shadow_map={},
        contradiction_map={},
        user_id="u",
        # recall() hands the page its (widened) filters; trust_tier is
        # never widened, so request.filters carries the same reading.
        filters=request.filters,
        # #2508: the acceptance step's queries would follow the resolver's;
        # this reads the resolver's target query, the last one without them.
        supersede_acceptance=False,
    )
    return _where(service)


TRUSTED = {"trust_tier": "trusted"}


@pytest.mark.asyncio
class TestResolver:
    async def test_a_trusted_read_holds_the_target_to_the_trusted_filter(self):
        target_id = uuid.uuid4()
        source = _source(target_id)
        service = _service(_target(target_id, SOURCE_TYPE_CONNECTOR))

        await service._resolve_supersede_candidates(
            {source.id: source}, user_id="u", operation="recall", filters=TRUSTED
        )

        sql = _where(service)
        assert TRUST_CONTEXT in sql
        assert f"{NOT_CONNECTOR} '{SOURCE_TYPE_CONNECTOR}'" in sql

    async def test_a_manual_target_the_query_returns_is_kept(self):
        # The trust rule lives in SQL only: a row the query returns is not
        # dropped again in Python.
        target_id = uuid.uuid4()
        source = _source(target_id)
        service = _service(_target(target_id, SOURCE_TYPE_MANUAL))

        resolved = await service._resolve_supersede_candidates(
            {source.id: source}, user_id="u", operation="recall", filters=TRUSTED
        )

        assert resolved[source.id].memory_id == target_id

    @pytest.mark.parametrize("filters", [None, {}, {"trust_tier": "all"}, {"type": "note"}])
    async def test_an_untrusted_read_adds_no_trust_predicate(self, filters):
        target_id = uuid.uuid4()
        source = _source(target_id)
        service = _service(_target(target_id, SOURCE_TYPE_CONNECTOR))

        resolved = await service._resolve_supersede_candidates(
            {source.id: source}, user_id="u", operation="recall", filters=filters
        )

        sql = _where(service)
        assert TRUST_CONTEXT not in sql
        assert NOT_CONNECTOR not in sql
        assert resolved[source.id].memory_id == target_id


@pytest.mark.asyncio
class TestRecallPage:
    async def test_a_trusted_recall_page_holds_its_targets_to_the_trusted_filter(self):
        sql = await _page_sql(RecallRequest(query="q", k=5, filters=TRUSTED))

        assert TRUST_CONTEXT in sql
        assert f"{NOT_CONNECTOR} '{SOURCE_TYPE_CONNECTOR}'" in sql

    async def test_an_unfiltered_recall_page_does_not(self):
        sql = await _page_sql(RecallRequest(query="q", k=5))

        assert TRUST_CONTEXT not in sql
        assert NOT_CONNECTOR not in sql


@pytest.mark.asyncio
class TestBootstrapRecall:
    async def test_the_bootstrap_recall_request_holds_targets_to_the_trusted_filter(self):
        # The request bootstrap hands MemoryService.recall, fed to the page
        # builder that recall runs: the target query excludes connector rows.
        recall = AsyncMock(return_value=RecallResponse(results=[]))
        with patch("services.memory_service.MemoryService") as ms_cls:
            ms_cls.return_value.recall = recall
            await AgentBootstrapService(MagicMock())._recall(
                SimpleNamespace(id=CTX, workspace_id=WS),
                BootstrapParams(agent_id=uuid.uuid4(), query="q"),
                BootstrapPrincipal(user_id="u", workspace_id=WS, principal_type="agent"),
            )
        request = recall.await_args.args[0]

        sql = await _page_sql(request)

        assert TRUST_CONTEXT in sql
        assert f"{NOT_CONNECTOR} '{SOURCE_TYPE_CONNECTOR}'" in sql


class TestIsTrustedRecall:
    """One reading of the trusted filter — the row filter and the
    selection-evidence guard ask here; the supersede resolver's SQL twin reads
    the same key the same way."""

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

    @pytest.mark.parametrize(
        "filters",
        [
            None,
            {},
            {"trust_tier": "all"},
            {"trust_tier": "trusted"},
            {"trust_tier": "trusted", "type": "note"},
        ],
    )
    def test_the_resolvers_sql_twin_agrees(self, filters):
        # #2257: the resolver reads trust from the same filters dict.
        from services.memory_listing import recall_trust_predicates
        from services.memory_service import _is_trusted_recall

        trusted = _is_trusted_recall(RecallRequest(query="q", filters=filters))
        assert bool(recall_trust_predicates(filters)) is trusted
