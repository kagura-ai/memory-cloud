"""Source provenance, budgets and rollback for the opt-in related index."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.memory import Memory
from models.sleep import SleepRelatedIndex
from services.recall_related_index import expand_recall
from services.sleep.related_index import (
    MAX_ADDITIONS,
    MAX_INDEX_PAIRS_PER_RUN,
    RelatedIndexPhase,
    candidate_links,
    current,
    evidence,
    undo_related_index,
)
from services.sleep.reporter import SleepBudget


def memory(**changes):
    values = {
        "id": uuid4(),
        "user_id": "fictional-owner",
        "workspace_id": uuid4(),
        "context_id": uuid4(),
        "summary": "Neri recommended the Luma workshop to Tavi.",
        "content": "Neri: I recommended Luma to Tavi on March 4.",
        "details": {"observed_at": "2024-03-05"},
        "version": 1,
        "type": "fact",
        "source_type": "manual",
        "source_uri": None,
        "deleted_at": None,
    }
    values.update(changes)
    return Memory(**values)


def pair():
    source = memory()
    target = memory(
        context_id=source.context_id,
        workspace_id=source.workspace_id,
        summary="Tavi had a guitar repaired at Luma.",
    )
    index = SleepRelatedIndex(
        edge_id=1,
        source_id=source.id,
        target_id=target.id,
        context_id=source.context_id,
        workspace_id=source.workspace_id,
        report_id=uuid4(),
        evidence={"source": evidence(source), "target": evidence(target)},
    )
    return source, target, index


def test_evidence_preserves_direction_and_dates_without_copying_source_text():
    source, target, index = pair()
    assert current(index, source, target)
    assert index.evidence["source"]["spans"] == [
        {"field": "summary", "start": 0, "end": len(source.summary)}
    ]
    assert "Neri" not in str(index.evidence)
    assert not current(index, target, source)
    source.details = {"observed_at": "2024-03-06"}
    assert not current(index, source, target)


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary", "Tavi recommended Luma to Neri."),
        ("content", "A corrected statement."),
        ("version", 2),
        ("deleted_at", "deleted"),
        ("context_id", uuid4()),
        ("workspace_id", uuid4()),
        ("source_type", "api"),
    ],
)
def test_source_changes_invalidate(field, value):
    source, target, index = pair()
    setattr(target, field, value)
    assert not current(index, source, target)


@pytest.mark.asyncio
async def test_disabled_builder_does_not_query(monkeypatch):
    monkeypatch.delenv("SLEEP_RELATED_INDEX_ENABLED", raising=False)
    db = MagicMock()
    result = await RelatedIndexPhase(db).execute(
        None, "owner", str(uuid4()), str(uuid4()), SleepBudget(), report_id=uuid4()
    )
    assert result.skipped
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_builder_is_bounded_incremental_and_audited(monkeypatch):
    monkeypatch.setenv("SLEEP_RELATED_INDEX_ENABLED", "true")
    source, target, _ = pair()
    edge = SimpleNamespace(id=1, src_id=source.id, dst_id=target.id)
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: [(edge,)]))
    db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [source, target]))
    reporter = SimpleNamespace(add_action=AsyncMock())
    phase = RelatedIndexPhase(db)
    args = (None, source.user_id, str(source.workspace_id), str(source.context_id))
    report_id = uuid4()
    result = await phase.execute(
        *args, SleepBudget(max_memories=8), reporter=reporter, report_id=report_id
    )
    assert result.memories_processed == 2
    index = db.add.call_args.args[0]
    assert current(index, source, target)
    assert reporter.add_action.await_args.kwargs["action_type"] == "index_related"
    # An indexed edge is never selected again: a repeat run charges no budget.
    db.execute.return_value = SimpleNamespace(all=lambda: [])
    reporter.add_action.reset_mock()
    result = await phase.execute(
        *args, SleepBudget(max_memories=8), reporter=reporter, report_id=uuid4()
    )
    assert result.details["indexed"] == 0
    assert result.memories_processed == 0
    reporter.add_action.assert_not_awaited()
    original_evidence = index.evidence
    target.content = "An unrelated replacement after discovery."
    result = await phase.execute(
        *args, SleepBudget(max_memories=8), reporter=reporter, report_id=uuid4()
    )
    assert result.details["indexed"] == 0
    assert index.evidence == original_evidence
    assert not current(index, source, target)
    db.execute.reset_mock()
    result = await phase.execute(*args, SleepBudget(max_memories=1), report_id=uuid4())
    assert result.skip_reason == "budget_exhausted"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_does_not_remove_a_later_rebuild():
    source, target, index = pair()
    db = SimpleNamespace(scalar=AsyncMock(return_value=index), delete=AsyncMock())
    action = SimpleNamespace(
        details={"edge_id": 1, "previous": None},
        report_id=uuid4(),
        memory_id=source.id,
        target_id=target.id,
    )
    ctx = SimpleNamespace(ctx_id_str=str(source.context_id), ws_id=str(source.workspace_id))
    await undo_related_index(db, action, ctx, {})
    db.delete.assert_not_awaited()
    action.report_id = index.report_id
    summary = {}
    await undo_related_index(db, action, ctx, summary)
    db.delete.assert_awaited_once_with(index)
    assert summary["related_indexes_reverted"] == 1


@asynccontextmanager
async def savepoint():
    yield


def reader(source, targets):
    db = SimpleNamespace(
        begin_nested=savepoint,
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: targets)),
    )

    @asynccontextmanager
    async def isolated():
        yield db

    service = SimpleNamespace(
        _isolated_session=isolated,
        db=db,
        _apply_supersede_shadowing=AsyncMock(return_value=({}, {})),
        _apply_supersede_fold=AsyncMock(return_value={}),
    )
    args = {
        "request": SimpleNamespace(search_mode="hybrid", include_superseded=False),
        "hits": [{"id": str(source.id), "score": 0.8}],
        "memories": {str(source.id): source},
        "user_id": source.user_id,
        "context_id": source.context_id,
        "workspace_id": source.workspace_id,
        "filters": None,
        "cluster_memory_ids": None,
    }
    return service, args


@pytest.mark.asyncio
async def test_reader_checks_provenance_permissions_and_new_candidate_cap(monkeypatch):
    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    source, target, index = pair()
    targets, indexes = [target], [index]
    for number in range(6):
        extra = memory(context_id=source.context_id, workspace_id=source.workspace_id)
        targets.append(extra)
        indexes.append(
            SleepRelatedIndex(
                edge_id=number + 2,
                source_id=source.id,
                target_id=extra.id,
                context_id=source.context_id,
                workspace_id=source.workspace_id,
                evidence={"source": evidence(source), "target": evidence(extra)},
            )
        )
    service, args = reader(source, targets)
    with (
        patch("services.recall_related_index.candidate_links", AsyncMock(return_value=indexes)),
        patch(
            "services.agent_binding_service.filter_memory_rows_by_binding",
            AsyncMock(return_value=(targets, 0)),
        ),
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ) as access,
    ):
        hits, rows = await expand_recall(service, **args)
        assert len(hits) == len(rows) == MAX_ADDITIONS
        assert all(hit["_related_index"] for hit in hits)
        assert access.await_count == MAX_ADDITIONS
        assert len(args["hits"]) == 1  # caller only commits additions on success
        access.return_value = False
        assert await expand_recall(service, **args) == ([], {})
        access.return_value = True
        for item in targets:
            item.content = "Changed after the index was built."
        assert await expand_recall(service, **args) == ([], {})


@pytest.mark.asyncio
async def test_disabled_unsupported_and_failed_index_preserve_baseline(monkeypatch):
    source, target, _ = pair()
    service, args = reader(source, [target])
    monkeypatch.delenv("RECALL_RELATED_INDEX_ENABLED", raising=False)
    assert await expand_recall(service, **args) == ([], {})
    service.db.scalars.assert_not_awaited()
    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    args["filters"] = {"near": {"lat": 1, "lon": 2}}
    assert await expand_recall(service, **args) == ([], {})
    args["filters"] = None
    with patch(
        "services.recall_related_index.candidate_links", AsyncMock(side_effect=RuntimeError)
    ):
        assert await expand_recall(service, **args) == ([], {})


@pytest.mark.asyncio
async def test_context_filter_is_resolved_and_does_not_disable_expansion(monkeypatch):
    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    source, target, index = pair()
    service, args = reader(source, [target])
    args["filters"] = {"context_id": str(source.context_id), "type": "fact"}
    with (
        patch("services.recall_related_index.candidate_links", AsyncMock(return_value=[index])),
        patch(
            "services.agent_binding_service.filter_memory_rows_by_binding",
            AsyncMock(return_value=([target], 0)),
        ),
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
    ):
        hits, rows = await expand_recall(service, **args)
    assert list(rows) == [str(target.id)]
    assert len(hits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", ["context", "workspace", "deleted", "binding"])
async def test_reader_rejects_ineligible_link_endpoints(monkeypatch, mutate):
    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    source, target, index = pair()
    service, args = reader(source, [target])
    if mutate == "context":
        target.context_id = uuid4()
    elif mutate == "workspace":
        target.workspace_id = uuid4()
    elif mutate == "deleted":
        target.deleted_at = "deleted"
    targets = [] if mutate == "binding" else [target]
    with (
        patch("services.recall_related_index.candidate_links", AsyncMock(return_value=[index])),
        patch(
            "services.agent_binding_service.filter_memory_rows_by_binding",
            AsyncMock(return_value=(targets, 0)),
        ),
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
    ):
        assert await expand_recall(service, **args) == ([], {})


@pytest.mark.asyncio
async def test_reader_timeout_preserves_input_pool(monkeypatch):
    import asyncio

    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    monkeypatch.setenv("RECALL_RELATED_INDEX_TIMEOUT_MS", "25")
    source, target, _ = pair()
    service, args = reader(source, [target])

    async def slow(*a, **kw):
        await asyncio.sleep(1)

    with patch("services.recall_related_index.candidate_links", slow):
        assert await expand_recall(service, **args) == ([], {})
    assert args["hits"] == [{"id": str(source.id), "score": 0.8}]


@pytest.mark.asyncio
async def test_link_query_binds_scope_and_excludes_live_supersession():
    from sqlalchemy.dialects import postgresql

    source, _, _ = pair()
    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    await candidate_links(
        db, [source], context_id=source.context_id, workspace_id=source.workspace_id
    )
    sql = str(
        db.scalars.call_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "NOT (EXISTS" in sql
    assert "supersedes" in sql and "deleted_at IS NULL" in sql
    assert str(source.context_id) in sql and str(source.workspace_id) in sql
    assert "LIMIT 16" in sql


@pytest.mark.asyncio
async def test_rollback_restores_previous_index_revision():
    source, target, index = pair()
    previous_report = uuid4()
    previous_evidence = {"source": {"version": 0}, "target": {"version": 0}}
    action = SimpleNamespace(
        details={
            "edge_id": 1,
            "previous": {"report_id": str(previous_report), "evidence": previous_evidence},
        },
        report_id=index.report_id,
        memory_id=source.id,
        target_id=target.id,
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=index), delete=AsyncMock())
    ctx = SimpleNamespace(ctx_id_str=str(source.context_id), ws_id=str(source.workspace_id))
    await undo_related_index(db, action, ctx, {})
    assert index.report_id == previous_report and index.evidence == previous_evidence
    db.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("derived_newer", [True, False])
async def test_cross_pool_fold_preserves_baseline_scores(monkeypatch, derived_newer):
    from services.memory_service import MemoryService
    from tests.services.test_supersede_fold import _cand, _judged, _mem, _pool, _run_recall

    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    newer, older = _mem(0), _mem(1)
    _judged(newer, older)
    baseline, derived = (older, newer) if derived_newer else (newer, older)
    hits, memories = _pool((baseline, 0.2))
    extra_hits, extra_memories = _pool((derived, 0.9))
    extra_hits[0]["_related_index"] = True
    service = MemoryService(db=AsyncMock())
    with patch(
        "services.recall_related_index.expand_recall",
        AsyncMock(return_value=(extra_hits, extra_memories)),
    ):
        build = await _run_recall(
            service,
            hits,
            memories,
            {newer.id: _cand(older)},
            neural_enabled=True,
            related_index=True,
        )
    assert [r["id"] for r in build.await_args.args[1]] == [str(newer.id)]
    assert service._recall_finalize.await_args.kwargs["baseline_pool"] == [
        {"id": str(baseline.id), "score": 0.2, "hybrid_score": 0.2}
    ]
    service._recall_run_hebbian_learning.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_expansion_keeps_normal_learning(monkeypatch):
    from services.memory_service import MemoryService
    from tests.services.test_supersede_fold import _mem, _pool, _run_recall

    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    hits, memories = _pool((_mem(0), 0.8), (_mem(1), 0.7))
    service = MemoryService(db=AsyncMock())
    with patch("services.recall_related_index.expand_recall", AsyncMock(return_value=([], {}))):
        await _run_recall(service, hits, memories, {}, neural_enabled=True, related_index=True)
    service._recall_run_hebbian_learning.assert_awaited_once()
    assert service._recall_run_hebbian_learning.await_args.args[1] == hits


def _compiled(statement) -> str:
    from sqlalchemy.dialects import postgresql

    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


@pytest.mark.asyncio
async def test_recall_expands_only_when_the_caller_opts_in(monkeypatch):
    """forget(query=...) deletes whatever recall returns: it must never widen (#2738)."""
    from services.memory_service import MemoryService
    from tests.services.test_supersede_fold import _mem, _pool, _run_recall

    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    hits, memories = _pool((_mem(0), 0.8))
    service = MemoryService(db=AsyncMock())
    with patch(
        "services.recall_related_index.expand_recall", AsyncMock(return_value=([], {}))
    ) as expand:
        await _run_recall(service, hits, memories, {})
        expand.assert_not_awaited()
        await _run_recall(service, hits, memories, {}, related_index=True)
        expand.assert_awaited_once()


def test_only_the_recall_tools_opt_into_the_related_index():
    import inspect
    from pathlib import Path

    from services.memory_service import MemoryService

    assert inspect.signature(MemoryService.recall).parameters["related_index"].default is False
    src = Path(__file__).resolve().parents[3] / "src"
    callers = {
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "related_index=True" in path.read_text(encoding="utf-8")
    }
    # The MCP recall tool and REST /memory/recall. Never forget-by-query,
    # bootstrap or share-key reads.
    assert callers == {"mcp_server/tools/memory.py", "api/routes/memory.py"}


@pytest.mark.asyncio
async def test_builder_skips_indexed_edges_and_takes_a_bounded_share(monkeypatch):
    monkeypatch.setenv("SLEEP_RELATED_INDEX_ENABLED", "true")
    source, _, _ = pair()
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
    db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
    args = (None, source.user_id, str(source.workspace_id), str(source.context_id))
    result = await RelatedIndexPhase(db).execute(
        *args, SleepBudget(max_memories=200, memories_used=30), report_id=uuid4()
    )
    sql = _compiled(db.execute.call_args.args[0])
    assert "sleep_related_indexes.edge_id IS NULL" in sql
    # A quarter of the 170 remaining memories at two per pair -> 21 pairs.
    assert "LIMIT 21" in sql
    assert sql.count("embedding_suppressed_at IS NULL") == 2
    assert result.memories_processed == 0
    db.execute.reset_mock()
    await RelatedIndexPhase(db).execute(*args, SleepBudget(max_memories=10_000), report_id=uuid4())
    assert f"LIMIT {MAX_INDEX_PAIRS_PER_RUN}" in _compiled(db.execute.call_args.args[0])


@pytest.mark.asyncio
async def test_reader_excludes_suppressed_endpoints(monkeypatch):
    monkeypatch.setenv("RECALL_RELATED_INDEX_ENABLED", "true")
    source, target, index = pair()
    service, args = reader(source, [target])
    with (
        patch("services.recall_related_index.candidate_links", AsyncMock(return_value=[index])),
        patch(
            "services.agent_binding_service.filter_memory_rows_by_binding",
            AsyncMock(return_value=([target], 0)),
        ),
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
    ):
        await expand_recall(service, **args)
    assert "embedding_suppressed_at IS NULL" in _compiled(service.db.scalars.call_args.args[0])
