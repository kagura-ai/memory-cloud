"""Coverage-focused tests for Sleep Maintenance Phase 4: Consolidation.

Complements ``test_consolidation.py`` by targeting the branches that the
house-style suite leaves uncovered:

- The LLM borderline path of ``execute()``: batch promote, an archive verdict
  that changes nothing (#3021), unknown-memory decision skip, budget
  exhaustion break, and the LLM-disabled no-op.
- ``_llm_judge_batch``: happy parse, invalid label / action filtering, the
  ``complete_json`` exception fallback (returns {}), token/breakdown
  accumulation, and the per-batch budget consume.
- ``_record_action`` with and without a reporter/report_id.

All external I/O (LLM service, GraphService) is mocked; the DB
is the ``AsyncMock`` repo from the source's own seam so no network call is made.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.consolidation import (
    BATCH_SIZE,
    ConsolidationPhase,
)
from services.sleep.reporter import LLMCallBreakdown, SleepBudget
from utils.datetime import utcnow


# --------------------------------------------------------------------------- #
# Builders / helpers
# --------------------------------------------------------------------------- #
def _make_config(provider="openai", model="gpt-5-nano"):
    config = MagicMock()
    config.sleep_llm_provider = provider
    config.sleep_llm_model = model
    return config


def _make_memory(
    *,
    memory_id=None,
    importance=0.3,
    access_count=0,
    reference_count=0,
    age_days=10,
    created_at=None,
    mtype="note",
    summary="borderline working memory",
):
    """A working memory that, with the default args, falls into the borderline
    bucket (no rule promotes/deletes it) so the LLM path is exercised."""
    m = MagicMock()
    m.id = memory_id or uuid4()
    m.summary = summary
    m.type = mtype
    m.importance = importance
    m.access_count = access_count
    m.reference_count = reference_count
    m.scope = "working"
    m.created_at = created_at if created_at is not None else (utcnow() - timedelta(days=age_days))
    return m


def _make_llm_response(decisions, *, total_tokens=42):
    """A stand-in for services.llm_service.LLMResponse."""
    resp = MagicMock()
    resp.parsed = {"decisions": decisions}
    resp.total_tokens = total_tokens
    resp.input_tokens = 30
    resp.output_tokens = 10
    resp.cached_input_tokens = 2
    resp.cache_write_tokens = 0
    resp.provider = "openai"
    resp.model = "gpt-5-nano"
    resp.tokenizer_version = "o200k_base"
    return resp


def _build_phase(memories):
    """Construct a ConsolidationPhase wired with AsyncMock repo + LLM."""
    db = AsyncMock()
    llm = AsyncMock()
    with patch("services.sleep.consolidation.MemoryRepository"):
        phase = ConsolidationPhase(db, llm)
    phase.memory_repo = AsyncMock()
    phase.memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=list)
    phase.memory_repo.soft_delete = AsyncMock(return_value=1)
    phase._fetch_working_memories = AsyncMock(return_value=memories)
    return phase, llm


async def _run_with_graph(phase, *, total_edges=0, node_metrics=None, config=None):
    """Drive execute() with a controllable GraphService."""
    config = config or _make_config(provider="")
    budget = SleepBudget()
    graph = MagicMock()
    graph.stats = AsyncMock(return_value={"total_edges": total_edges})
    graph.get_node_metrics = AsyncMock(return_value=node_metrics)
    with patch("services.sleep.consolidation.GraphService", return_value=graph):
        result = await phase.execute(config, "user-1", "ws-1", "ctx-1", budget)
    return result, budget, graph


# --------------------------------------------------------------------------- #
# LLM borderline path of execute()
# --------------------------------------------------------------------------- #
class TestLLMBorderlinePath:
    """The borderline → LLM judgment branch of execute()."""

    @pytest.mark.asyncio
    async def test_llm_disabled_leaves_borderline_untouched(self):
        mem = _make_memory()
        phase, llm = _build_phase([mem])
        result, _, _ = await _run_with_graph(phase, config=_make_config(provider=""))

        assert result.details["borderline"] == 1
        assert result.details["llm_promoted"] == 0
        assert result.details["llm_archived"] == 0
        phase.memory_repo.bulk_promote_to_persistent.assert_not_called()
        phase.memory_repo.soft_delete.assert_not_called()
        # complete_json must never be reached when the provider is empty.
        llm.complete_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_promote_decision_promotes(self):
        mem = _make_memory()
        phase, llm = _build_phase([mem])
        # decisions reference label "A" (single-element batch → only label).
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response([{"label": "A", "action": "promote"}])
        )
        result, budget, _ = await _run_with_graph(phase, config=_make_config(provider="openai"))

        assert result.details["llm_promoted"] == 1
        assert result.details["llm_archived"] == 0
        phase.memory_repo.bulk_promote_to_persistent.assert_awaited_once_with([mem.id])
        assert mem.id in result.changed_memory_ids
        # #2015 D5: a promotion is metadata only (the reindex syncs its scope).
        assert result.content_changed_memory_ids == set()
        # tokens + budget consumed by the single batch call.
        assert result.tokens_used == 42
        assert budget.llm_calls_used == 1
        assert result.llm_calls_used == 1

    @pytest.mark.asyncio
    async def test_llm_archive_response_dropped_at_parse(self):
        # #1233: "archive" is no longer offered to the judge — an LLM
        # response that uses it anyway is dropped at PARSE, so nothing is
        # deleted and nothing is guarded (the guard counter only moves for
        # decisions that reach the execute loop, which parse now prevents).
        # Pre-#1229 this test asserted the LLM could delete a fresh memory;
        # #1229 turned that into a guarded refusal; #1233 removes the
        # option at the source.
        mem = _make_memory()
        phase, llm = _build_phase([mem])
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response([{"label": "A", "action": "archive"}])
        )
        result, _, _ = await _run_with_graph(phase, total_edges=0, config=_make_config())

        assert result.details["llm_archived"] == 0
        assert result.details["llm_archive_guarded"] == 0
        phase.memory_repo.soft_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_archive_verdict_on_an_isolated_old_memory_changes_nothing(self):
        # #3021: should any decision source answer "archive" (the parser drops
        # it; bypass the parser to pin the loop), an old, unadopted, isolated
        # memory stays working — nothing archives, nothing is counted.
        mem = _make_memory(age_days=400)
        phase, _ = _build_phase([mem])
        phase._llm_judge_batch = AsyncMock(return_value={mem.id: "archive"})
        isolated = {
            "centrality": 0.0,
            "edge_count": 0,
            "avg_edge_weight": 0.0,
            "is_hub_node": False,
            "is_isolated": True,
        }
        result, _, _ = await _run_with_graph(
            phase, total_edges=5, node_metrics=isolated, config=_make_config()
        )

        assert result.details["llm_archived"] == 0
        assert result.details["llm_archive_guarded"] == 0
        phase.memory_repo.soft_delete.assert_not_called()
        phase.memory_repo.bulk_promote_to_persistent.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_keep_decision_is_noop(self):
        mem = _make_memory()
        phase, llm = _build_phase([mem])
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response([{"label": "A", "action": "keep"}])
        )
        result, _, _ = await _run_with_graph(phase, config=_make_config())

        assert result.details["llm_promoted"] == 0
        assert result.details["llm_archived"] == 0
        phase.memory_repo.bulk_promote_to_persistent.assert_not_called()
        phase.memory_repo.soft_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_exhausted_skips_llm_batches(self):
        mem = _make_memory()
        phase, llm = _build_phase([mem])
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response([{"label": "A", "action": "promote"}])
        )
        graph = MagicMock()
        graph.stats = AsyncMock(return_value={"total_edges": 0})
        graph.get_node_metrics = AsyncMock(return_value=None)
        # Budget already at the cap → can_afford(llm_calls=1) is False → break.
        budget = SleepBudget(max_llm_calls=0)
        with (
            patch("services.sleep.consolidation.GraphService", return_value=graph),
        ):
            result = await phase.execute(_make_config(), "u", "ws", "ctx", budget)

        assert result.details["llm_promoted"] == 0
        llm.complete_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_memory_id_in_decisions_is_skipped(self):
        # The LLM returns a label that maps to a real id in label_to_id, but we
        # monkeypatch _llm_judge_batch to return an id NOT in the batch_map so
        # the "unknown_memory" continue branch executes.
        mem = _make_memory()
        phase, _ = _build_phase([mem])
        stray_id = uuid4()
        phase._llm_judge_batch = AsyncMock(return_value={stray_id: "promote"})
        result, _, _ = await _run_with_graph(phase, config=_make_config())

        assert result.details["llm_promoted"] == 0
        phase.memory_repo.bulk_promote_to_persistent.assert_not_called()

    @pytest.mark.asyncio
    async def test_two_batches_processed(self):
        # More than BATCH_SIZE borderline memories → two LLM batch calls.
        n = BATCH_SIZE + 2
        mems = [_make_memory() for _ in range(n)]
        phase, llm = _build_phase(mems)
        # Promote every memory the batch knows about. The batch labels are
        # A..E then A..B, so respond per-call with all 26 possible labels; the
        # parser keeps only those valid for that batch.
        all_labels = [{"label": chr(ord("A") + i), "action": "promote"} for i in range(26)]
        llm.complete_json = AsyncMock(return_value=_make_llm_response(all_labels))
        result, budget, _ = await _run_with_graph(phase, config=_make_config())

        assert result.details["borderline"] == n
        assert result.details["llm_promoted"] == n
        assert budget.llm_calls_used == 2  # ceil(7 / 5)
        assert (
            sum(
                len(c.args[0]) for c in phase.memory_repo.bulk_promote_to_persistent.await_args_list
            )
            == n
        )


# --------------------------------------------------------------------------- #
# _llm_judge_batch unit behavior
# --------------------------------------------------------------------------- #
class TestLLMJudgeBatch:
    """Direct exercises of ``_llm_judge_batch`` parsing + side effects."""

    @pytest.mark.asyncio
    async def test_valid_decisions_mapped_to_ids(self):
        mems = [_make_memory(), _make_memory()]
        phase, llm = _build_phase(mems)
        phase._tokens_used = 0
        phase._llm_breakdown = None
        # Force a deterministic label→memory mapping by stubbing the shuffle.
        with patch("services.sleep.consolidation.random.shuffle", lambda x: None):
            llm.complete_json = AsyncMock(
                return_value=_make_llm_response(
                    [
                        {"label": "A", "action": "promote"},
                        {"label": "B", "action": "keep"},
                    ]
                )
            )
            budget = SleepBudget()
            decisions = await phase._llm_judge_batch(mems, "u", "ctx", "ws", budget, _make_config())

        # No shuffle → label A is mems[0], B is mems[1].
        assert decisions[mems[0].id] == "promote"
        assert decisions[mems[1].id] == "keep"
        assert budget.llm_calls_used == 1
        assert phase._tokens_used == 42

    @pytest.mark.asyncio
    async def test_invalid_action_filtered(self):
        # Two-element batch: label A has an invalid action (filtered via the
        # action ``continue``), label B is valid (kept) — proves the invalid one
        # is skipped while a sibling valid decision in the same response survives.
        mems = [_make_memory(), _make_memory()]
        phase, llm = _build_phase(mems)
        phase._tokens_used = 0
        phase._llm_breakdown = None
        with patch("services.sleep.consolidation.random.shuffle", lambda x: None):
            llm.complete_json = AsyncMock(
                return_value=_make_llm_response(
                    [
                        {"label": "A", "action": "destroy"},  # invalid → dropped
                        {"label": "B", "action": "promote"},  # valid → kept
                    ]
                )
            )
            decisions = await phase._llm_judge_batch(
                mems, "u", "ctx", "ws", SleepBudget(), _make_config()
            )
        # No shuffle → A is mems[0], B is mems[1].
        assert mems[0].id not in decisions
        assert decisions[mems[1].id] == "promote"
        assert len(decisions) == 1

    @pytest.mark.asyncio
    async def test_unknown_label_filtered(self):
        mems = [_make_memory()]
        phase, llm = _build_phase(mems)
        phase._tokens_used = 0
        with patch("services.sleep.consolidation.random.shuffle", lambda x: None):
            llm.complete_json = AsyncMock(
                return_value=_make_llm_response([{"label": "Z", "action": "promote"}])
            )
            decisions = await phase._llm_judge_batch(
                mems, "u", "ctx", "ws", SleepBudget(), _make_config()
            )
        assert decisions == {}

    @pytest.mark.asyncio
    async def test_missing_label_or_action_keys_filtered(self):
        mems = [_make_memory()]
        phase, llm = _build_phase(mems)
        phase._tokens_used = 0
        with patch("services.sleep.consolidation.random.shuffle", lambda x: None):
            llm.complete_json = AsyncMock(
                return_value=_make_llm_response([{"reason": "no label or action"}])
            )
            decisions = await phase._llm_judge_batch(
                mems, "u", "ctx", "ws", SleepBudget(), _make_config()
            )
        assert decisions == {}

    @pytest.mark.asyncio
    async def test_llm_exception_returns_empty_and_no_budget_consumed(self):
        mems = [_make_memory()]
        phase, llm = _build_phase(mems)
        phase._tokens_used = 0
        phase._llm_breakdown = None
        llm.complete_json = AsyncMock(side_effect=RuntimeError("provider down"))
        budget = SleepBudget()
        decisions = await phase._llm_judge_batch(mems, "u", "ctx", "ws", budget, _make_config())
        assert decisions == {}
        # The except branch returns before budget.consume / token accumulation.
        assert budget.llm_calls_used == 0
        assert phase._tokens_used == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_usage", [True, False])
    async def test_failed_call_usage_is_counted(self, failed_llm_error, with_usage):
        """#2038: a failed call's spent tokens reach the totals; calls stay 0."""
        mems = [_make_memory()]
        phase, llm = _build_phase(mems)
        phase._tokens_used = 0
        phase._llm_breakdown = None
        phase._llm_failures = 0
        llm.complete_json = AsyncMock(side_effect=failed_llm_error(with_usage))
        budget = SleepBudget()

        decisions = await phase._llm_judge_batch(mems, "u", "ctx", "ws", budget, _make_config())

        assert decisions == {}
        assert phase._llm_failures == 1
        # A billed failure counts against the budget; an unbilled one does not.
        assert budget.llm_calls_used == (1 if with_usage else 0)
        if with_usage:
            assert phase._tokens_used == 70
            assert phase._llm_breakdown is not None
            assert phase._llm_breakdown.total_tokens == 70
            assert phase._llm_breakdown.calls == 0
        else:
            assert phase._tokens_used == 0
            assert phase._llm_breakdown is None

    @pytest.mark.asyncio
    async def test_token_and_breakdown_accumulation(self):
        mems = [_make_memory()]
        phase, llm = _build_phase(mems)
        phase._tokens_used = 0
        phase._llm_breakdown = None
        resp = _make_llm_response([{"label": "A", "action": "keep"}], total_tokens=99)
        llm.complete_json = AsyncMock(return_value=resp)
        budget = SleepBudget()
        await phase._llm_judge_batch(mems, "u", "ctx", "ws", budget, _make_config())

        assert phase._tokens_used == 99
        assert budget.llm_calls_used == 1
        assert isinstance(phase._llm_breakdown, LLMCallBreakdown)
        assert phase._llm_breakdown.provider == "openai"
        assert phase._llm_breakdown.model == "gpt-5-nano"
        assert phase._llm_breakdown.calls == 1
        assert phase._llm_breakdown.input_tokens == 30
        assert phase._llm_breakdown.output_tokens == 10


# --------------------------------------------------------------------------- #
# _record_action
# --------------------------------------------------------------------------- #
class TestRecordAction:
    """The static reporter helper — gated on (reporter and report_id)."""

    @pytest.mark.asyncio
    async def test_records_when_reporter_and_report_id_present(self):
        reporter = AsyncMock()
        report_id = uuid4()
        memory_id = uuid4()
        await ConsolidationPhase._record_action(
            reporter,
            report_id,
            "promote",
            memory_id,
            "rule",
            0.7,
            4,
            12,
            3,
        )
        reporter.add_action.assert_awaited_once()
        _, kwargs = reporter.add_action.call_args
        assert kwargs["report_id"] == report_id
        assert kwargs["phase"] == "consolidation"
        assert kwargs["action_type"] == "promote"
        assert kwargs["memory_id"] == memory_id
        assert kwargs["details"]["reason"] == "rule"
        assert kwargs["details"]["reference_count"] == 3
        assert kwargs["details"]["access_count"] == 4
        assert kwargs["details"]["age_days"] == 12
        assert kwargs["details"]["importance"] == 0.7

    @pytest.mark.asyncio
    async def test_noop_when_reporter_missing(self):
        # report_id present but reporter None → nothing recorded, no error.
        await ConsolidationPhase._record_action(
            None, uuid4(), "archive", uuid4(), "llm", 0.1, 0, 40
        )

    @pytest.mark.asyncio
    async def test_noop_when_report_id_missing(self):
        reporter = AsyncMock()
        await ConsolidationPhase._record_action(
            reporter, None, "archive", uuid4(), "llm", 0.1, 0, 40
        )
        reporter.add_action.assert_not_called()


# --------------------------------------------------------------------------- #
# execute() reporter integration + result roll-up
# --------------------------------------------------------------------------- #
class TestExecuteReporterAndRollup:
    """execute() wired with a reporter to cover the _record_action call path
    and the llm_breakdown attachment on the result."""

    @pytest.mark.asyncio
    async def test_rule_promote_records_action_and_breakdown_absent(self):
        # A rule-promoted memory (adoption >= ADOPTION_PROMOTE_MIN) with a
        # reporter wired → add_action called with reason="rule"; no LLM →
        # llm_breakdown stays empty.
        mem = _make_memory(reference_count=5, importance=0.1, age_days=2)
        phase, _ = _build_phase([mem])
        reporter = AsyncMock()
        report_id = uuid4()
        budget = SleepBudget()
        graph = MagicMock()
        graph.stats = AsyncMock(return_value={"total_edges": 0})
        graph.get_node_metrics = AsyncMock(return_value=None)
        with (
            patch("services.sleep.consolidation.GraphService", return_value=graph),
        ):
            result = await phase.execute(
                _make_config(provider=""),
                "u",
                "ws",
                "ctx",
                budget,
                reporter=reporter,
                report_id=report_id,
            )

        assert result.details["rule_promoted"] == 1
        reporter.add_action.assert_awaited_once()
        _, kwargs = reporter.add_action.call_args
        assert kwargs["action_type"] == "promote"
        assert kwargs["details"]["reason"] == "rule"
        assert result.llm_breakdown == []
        assert result.memories_processed == 1

    @pytest.mark.asyncio
    async def test_llm_promote_attaches_breakdown_to_result(self):
        mem = _make_memory()
        phase, llm = _build_phase([mem])
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response([{"label": "A", "action": "promote"}])
        )
        result, _, _ = await _run_with_graph(phase, config=_make_config(provider="openai"))
        # Assert the LLM breakdown roll-up from the promote run.
        assert len(result.llm_breakdown) == 1
        assert result.llm_breakdown[0].calls == 1
        assert result.llm_breakdown[0].provider == "openai"


# --------------------------------------------------------------------------- #
# _fetch_working_memories against the real DB (db_session)
# --------------------------------------------------------------------------- #
def _persist_memory(**overrides):
    """Build a real Memory row with the NOT-NULL-without-default columns set."""
    from models.memory import Memory

    fields = {
        "id": uuid4(),
        "user_id": "fetch-user",
        "workspace_id": uuid4(),
        "summary": "real working memory",
        "content": "body",
        "type": "note",
        "client": "pytest",
        "scope": "working",
    }
    fields.update(overrides)
    return Memory(**fields)


class TestFetchWorkingMemories:
    """Exercise the real ``_fetch_working_memories`` SQL against db_session,
    covering the workspace/context filter branches and the no-results early
    return in execute()."""

    @pytest.mark.asyncio
    async def test_fetches_only_working_non_deleted_for_user(self, db_session):
        from models.memory import Memory

        user = f"u-{uuid4()}"
        working = _persist_memory(user_id=user, scope="working")
        persistent = _persist_memory(user_id=user, scope="persistent")
        deleted = _persist_memory(user_id=user, scope="working", deleted_at=utcnow())
        other_user = _persist_memory(user_id=f"other-{uuid4()}", scope="working")
        for row in (working, persistent, deleted, other_user):
            db_session.add(row)
        await db_session.flush()

        with patch("services.sleep.consolidation.MemoryRepository"):
            phase = ConsolidationPhase(db_session, AsyncMock())
        rows = await phase._fetch_working_memories(user, None, None)

        ids = {r.id for r in rows}
        assert working.id in ids
        assert persistent.id not in ids  # wrong scope
        assert deleted.id not in ids  # soft-deleted
        assert other_user.id not in ids  # wrong user
        assert all(isinstance(r, Memory) for r in rows)

    @pytest.mark.asyncio
    async def test_excludes_pinned_working_rows(self, db_session):
        """#1523: a pinned row can be working-scope (a pre-#1523 rollback demoted
        it, or the pin was set on a working row). Consolidation leaves it alone,
        so the shared exemption applies at the fetch."""
        user = f"u-{uuid4()}"
        plain = _persist_memory(user_id=user, scope="working")
        pinned = _persist_memory(user_id=user, scope="working", delivery_mode="always")
        db_session.add_all([plain, pinned])
        await db_session.flush()

        with patch("services.sleep.consolidation.MemoryRepository"):
            phase = ConsolidationPhase(db_session, AsyncMock())
        rows = await phase._fetch_working_memories(user, None, None)

        ids = {r.id for r in rows}
        assert plain.id in ids
        assert pinned.id not in ids

    @pytest.mark.asyncio
    async def test_excludes_tool_triggered_working_rows(self, db_session):
        """Tool guardrails share the pinned exemption: the fetch drops them."""
        user = f"u-{uuid4()}"
        plain = _persist_memory(user_id=user, scope="working", details={"other": 1})
        guardrail = _persist_memory(
            user_id=user,
            scope="working",
            details={
                "tool_trigger": {
                    "tool": "Bash",
                    "on": "pre",
                    "match": "gh pr merge",
                    "action": "inform",
                }
            },
        )
        db_session.add_all([plain, guardrail])
        await db_session.flush()

        with patch("services.sleep.consolidation.MemoryRepository"):
            phase = ConsolidationPhase(db_session, AsyncMock())
        rows = await phase._fetch_working_memories(user, None, None)

        ids = {r.id for r in rows}
        assert plain.id in ids
        assert guardrail.id not in ids

    @pytest.mark.asyncio
    async def test_workspace_filter_applied(self, db_session):
        # workspace_id has no FK → safe to set directly. Covers the workspace
        # ``stmt.where`` branch.
        user = f"u-{uuid4()}"
        ws_id = uuid4()
        match = _persist_memory(user_id=user, scope="working", workspace_id=ws_id)
        wrong_ws = _persist_memory(user_id=user, scope="working", workspace_id=uuid4())
        for row in (match, wrong_ws):
            db_session.add(row)
        await db_session.flush()

        with patch("services.sleep.consolidation.MemoryRepository"):
            phase = ConsolidationPhase(db_session, AsyncMock())
        rows = await phase._fetch_working_memories(user, str(ws_id), None)

        ids = {r.id for r in rows}
        assert match.id in ids
        assert wrong_ws.id not in ids

    @pytest.mark.asyncio
    async def test_context_filter_branch_executes(self, db_session):
        # context_id has an FK, so we don't persist a matching row; passing a
        # non-existent context_id simply exercises the context ``stmt.where``
        # branch and returns no rows (the NULL-context row does not match).
        user = f"u-{uuid4()}"
        present = _persist_memory(user_id=user, scope="working", context_id=None)
        db_session.add(present)
        await db_session.flush()

        with patch("services.sleep.consolidation.MemoryRepository"):
            phase = ConsolidationPhase(db_session, AsyncMock())
        rows = await phase._fetch_working_memories(user, None, str(uuid4()))

        assert rows == []

    @pytest.mark.asyncio
    async def test_execute_no_working_memories_early_return(self, db_session):
        # A user with zero working memories → execute() takes the early
        # "no_working_memories" return without touching the graph/LLM.
        with patch("services.sleep.consolidation.MemoryRepository"):
            phase = ConsolidationPhase(db_session, AsyncMock())
        result = await phase.execute(
            _make_config(provider=""), f"empty-{uuid4()}", None, None, SleepBudget()
        )
        assert result.details == {"message": "no_working_memories"}
        assert result.memories_processed == 0
