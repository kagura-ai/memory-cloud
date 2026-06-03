"""Recall ``trust_tier`` filter test (Issue #887).

Pins the security-relevant recall filter: when
``recall(filters={"trust_tier": "trusted"})`` is set, the candidate-fetch
SELECT must restrict to memories whose Context is authoritatively trusted
(``Context.trust_tier == 'trusted'``) and drop connector-sourced rows
(``source_type != 'connector'``). Without this pin, a regression that dropped
the predicate would silently re-open the prompt-injection surface this filter
closes (OWASP LLM01/LLM03).

Mirrors the mocked-service pattern of test_recall_analysis_cluster_filter.py:
``search_service.hybrid_search`` and ``db.execute`` are mocked, and we assert on
the compiled SQL of the ``select(Memory).where(*pg_conditions)`` candidate fetch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.schemas import RecallRequest


def _make_service():
    from services.memory_service import MemoryService

    db = AsyncMock()
    # db.execute(...) awaits to a result whose .scalars().all() → [] (the
    # candidate fetch at memory_service.py:1504) and whose .scalar_one_or_none()
    # is a truthy mock (context isolation). Same statement object is recorded in
    # call_args_list regardless, which is what _compiled_selects inspects.
    _result = MagicMock()
    _result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=_result)
    svc = MemoryService(db)
    svc.search_service = MagicMock()
    # Return one fake candidate so recall proceeds past the ``if not memory_ids``
    # early return to the ``select(Memory).where(*pg_conditions)`` candidate
    # fetch (the statement we assert on). The row won't resolve from the mocked
    # db.execute, so the response loop simply skips it — fine for this test.
    svc.search_service.hybrid_search = AsyncMock(return_value=[{"id": str(uuid4()), "score": 0.9}])
    svc.memory_repo = MagicMock()
    return svc, db


def _compiled_selects(db) -> str:
    """All statements passed to db.execute, compiled to SQL, joined."""
    sqls = []
    for call in db.execute.call_args_list:
        if call.args:
            try:
                sqls.append(str(call.args[0].compile(compile_kwargs={"literal_binds": False})))
            except Exception:
                sqls.append(str(call.args[0]))
    return " ".join(sqls)


@pytest.mark.asyncio
async def test_trusted_filter_adds_context_and_source_predicates():
    svc, db = _make_service()
    request = RecallRequest(query="q", k=5, filters={"trust_tier": "trusted"})

    await svc.recall(
        request=request,
        user_id="u",
        current_context_id=uuid4(),
        current_workspace_id=uuid4(),
    )

    sql = _compiled_selects(db)
    # Authoritative context-level check + row-level connector defense-in-depth.
    assert "trust_tier" in sql, "recall must consult Context.trust_tier when trusted"
    assert "source_type" in sql, "recall must also drop connector-sourced rows"


@pytest.mark.asyncio
async def test_no_trust_filter_omits_trust_predicates():
    svc, db = _make_service()
    request = RecallRequest(query="q", k=5)  # no filters

    await svc.recall(
        request=request,
        user_id="u",
        current_context_id=uuid4(),
        current_workspace_id=uuid4(),
    )

    sql = _compiled_selects(db)
    # Without the filter, the candidate SELECT must NOT carry the trust predicate
    # (default recall still returns connector/external memories for knowledge use).
    assert "trust_tier" not in sql
