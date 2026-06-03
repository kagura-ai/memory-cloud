"""End-to-end recall ``trust_tier`` filter test against a real DB (Issue #908).

Follow-up to #887 (PR #907). The service-level pin in
``tests/services/test_recall_trust_tier_filter.py`` asserts on the *compiled SQL*
of the candidate-fetch SELECT (mocked DB). That proves the predicate is emitted,
but it cannot prove the predicate actually *excludes the right rows* — in
particular the authoritative-layer case the three gate2 advisors converged on:

    a NON-connector row that lives in an ``external``-tier context must STILL be
    excluded, because ``Context.trust_tier`` is the server-side authority, not
    the per-row ``source_type`` proxy.

This test exercises the real ``recall()`` candidate-fetch path against real
``Context.trust_tier`` rows in Postgres. Only the search backend
(``search_service.hybrid_search``) is stubbed — it returns the candidate memory
IDs that a BM25/keyword match would surface, exactly as the issue prescribes
("BM25/keyword mode avoids the embedding stack"). ``search_mode="keyword"`` also
short-circuits the Hebbian/neural learning tail (memory_service.py:1548), so no
Qdrant / embedding / graph wiring is needed.

Scenario (single workspace, two contexts):
- ``trusted_ctx``   (trust_tier='trusted'): one manual memory  → ``mem_trusted``
- ``external_ctx``  (trust_tier='external'):
    - one connector memory      → ``mem_connector``    (row-level proxy would catch)
    - one MANUAL (non-connector) memory → ``mem_manual_external``
      (row-level proxy would MISS — only the authoritative context check catches it)

Assertions:
- ``recall(filters={'trust_tier':'trusted'})`` returns ONLY ``mem_trusted``;
  both external-context rows are excluded — including the manual one.
- ``recall()`` with no filter returns ALL THREE (default recall still surfaces
  connector/external memories for normal knowledge use — the exclusion is opt-in).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import (
    CONTEXT_TRUST_TIER_EXTERNAL,
    CONTEXT_TRUST_TIER_TRUSTED,
    Context,
    Workspace,
)
from models.memory import SOURCE_TYPE_CONNECTOR, SOURCE_TYPE_MANUAL, Memory
from models.schemas import RecallRequest


@pytest_asyncio.fixture
async def trust_tier_scenario(db_session: AsyncSession):
    """One workspace, a trusted context + an external context, three memories."""
    owner_id = f"owner_{uuid4().hex[:8]}"

    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    trusted_ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"trusted-{uuid4().hex[:8]}",
        created_by=owner_id,
        is_private=False,
        trust_tier=CONTEXT_TRUST_TIER_TRUSTED,
    )
    external_ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"external-{uuid4().hex[:8]}",
        created_by=owner_id,
        is_private=False,
        trust_tier=CONTEXT_TRUST_TIER_EXTERNAL,
    )

    def _mk_memory(*, context_id, source_type: str, label: str) -> Memory:
        return Memory(
            id=uuid4(),
            user_id=owner_id,
            workspace_id=ws.id,
            context_id=context_id,
            summary=f"{label}-{uuid4().hex[:6]}",
            content="trust tier recall probe",
            type="note",
            client="test",
            tags=[],
            source_type=source_type,
        )

    mem_trusted = _mk_memory(
        context_id=trusted_ctx.id, source_type=SOURCE_TYPE_MANUAL, label="trusted-manual"
    )
    mem_connector = _mk_memory(
        context_id=external_ctx.id, source_type=SOURCE_TYPE_CONNECTOR, label="external-connector"
    )
    # The authoritative-layer case: a manual (non-connector) row in an external
    # context. ``source_type != 'connector'`` alone would NOT exclude it.
    mem_manual_external = _mk_memory(
        context_id=external_ctx.id, source_type=SOURCE_TYPE_MANUAL, label="external-manual"
    )

    db_session.add(ws)
    await db_session.flush()
    db_session.add_all([trusted_ctx, external_ctx])
    await db_session.flush()
    db_session.add_all([mem_trusted, mem_connector, mem_manual_external])
    await db_session.flush()

    return {
        "owner_id": owner_id,
        "workspace_id": ws.id,
        "trusted_ctx_id": trusted_ctx.id,
        "external_ctx_id": external_ctx.id,
        "mem_trusted": mem_trusted,
        "mem_connector": mem_connector,
        "mem_manual_external": mem_manual_external,
    }


def _service_with_stubbed_search(db_session: AsyncSession, candidate_memories: list[Memory]):
    """Real MemoryService whose only stub is the search backend.

    ``hybrid_search`` returns the candidate IDs a BM25/keyword match would
    surface; everything downstream (the candidate-fetch SELECT carrying the
    trust predicate, access-stat updates) runs against the real DB.
    """
    from services.memory_service import MemoryService

    svc = MemoryService(db_session)
    search_results = [{"id": str(m.id), "score": 0.9} for m in candidate_memories]
    svc.search_service.hybrid_search = AsyncMock(return_value=search_results)
    return svc


async def _recall_ids(svc, *, query, owner_id, context_id, workspace_id, filters=None):
    """Run a keyword-mode recall and return the set of returned memory IDs (str)."""
    request = RecallRequest(query=query, k=10, search_mode="keyword", filters=filters)
    response = await svc.recall(
        request=request,
        user_id=owner_id,
        current_context_id=context_id,
        current_workspace_id=workspace_id,
    )
    return {str(r.memory_id) for r in response.results}


@pytest.mark.asyncio
async def test_trusted_filter_excludes_all_external_context_rows(
    db_session: AsyncSession, trust_tier_scenario
):
    """``trust_tier=trusted`` drops connector AND non-connector external rows."""
    s = trust_tier_scenario
    candidates = [s["mem_trusted"], s["mem_connector"], s["mem_manual_external"]]
    svc = _service_with_stubbed_search(db_session, candidates)

    returned = await _recall_ids(
        svc,
        query="trust tier recall probe",
        owner_id=s["owner_id"],
        context_id=s["trusted_ctx_id"],
        workspace_id=s["workspace_id"],
        filters={"trust_tier": "trusted"},
    )

    assert returned == {str(s["mem_trusted"].id)}, (
        "trusted filter must return only the trusted-context memory; both the "
        "connector row and the MANUAL row living in an external context must be "
        "excluded by the authoritative Context.trust_tier check"
    )
    # Explicit authoritative-layer assertion: the manual external row (which the
    # row-level source_type proxy would NOT catch) is gone.
    assert str(s["mem_manual_external"].id) not in returned
    assert str(s["mem_connector"].id) not in returned


@pytest.mark.asyncio
async def test_no_filter_returns_all_including_external(
    db_session: AsyncSession, trust_tier_scenario
):
    """Default recall (no trust filter) still surfaces connector/external rows."""
    s = trust_tier_scenario
    candidates = [s["mem_trusted"], s["mem_connector"], s["mem_manual_external"]]
    svc = _service_with_stubbed_search(db_session, candidates)

    returned = await _recall_ids(
        svc,
        query="trust tier recall probe",
        owner_id=s["owner_id"],
        context_id=s["trusted_ctx_id"],
        workspace_id=s["workspace_id"],
        filters=None,
    )

    assert returned == {
        str(s["mem_trusted"].id),
        str(s["mem_connector"].id),
        str(s["mem_manual_external"].id),
    }, "without the opt-in filter, recall must return all three rows (exclusion is opt-in)"
