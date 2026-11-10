"""Protocol checks only: these tests do not make live quality claims."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tests.eval import related_index_runner as runner
from tests.eval.related_index_cases import PROBES, SOURCES
from tests.eval.related_index_runner import (
    SKIP_EVENT,
    SkipLog,
    aggregate,
    answer_problem,
    git_state,
    local_only,
    mcp_evidence,
    offline_usage,
    opaque_documents,
    pair_summary,
    percentile,
    score,
    trial_order,
    validate_config,
    verdict,
)


def test_answer_scoring_requires_exact_facts_and_actual_retrieved_sources():
    probe = PROBES[1]
    ids = {"a": "recommendation", "b": "repair", "c": "distractor"}
    answer = {"facts": probe["facts"], "source_ids": ["a", "b"]}
    assert score(probe, {"recommendation", "repair"}, answer, ids)["grounded_correct"] == 1
    assert score(probe, {"repair"}, answer, ids)["grounded_correct"] == 0
    assert (
        score(probe, {"recommendation", "repair"}, {**answer, "source_ids": ["a", "b", "c"]}, ids)[
            "grounded_correct"
        ]
        == 0
    )
    assert score(PROBES[-1], set(), {"facts": {}, "source_ids": []}, ids)["grounded_correct"] == 1


def test_source_split_and_metrics_are_deterministic():
    assert all(set(p["sources"]) <= SOURCES.keys() for p in PROBES)
    # Even an unanswerable question asks for a field; the prompt cannot reveal
    # abstention by deriving an empty field list from the scoring labels.
    assert all(p["fields"] for p in PROBES)
    assert not any(p["query"] in text for p in PROBES for text in SOURCES.values())
    assert percentile([1, 2, 3, 4], 0.5) == 2
    assert percentile([1, 2, 3, 4], 0.95) == 4
    row = {
        "required_source_coverage": 1,
        "irrelevant_evidence": 0,
        "grounded_correct": 1,
        "response_tokens": 30,
        "online_reported_tokens": 40,
        "online_usd": 0,
        "tool_calls": 1,
        "embedding_calls": 1,
        "answer_calls": 1,
        "extra_candidates": 1,
        "extras_in_evidence": 1,
        "expansion_skips": 0,
        "answer_errors": 0,
        "retrieval_ms": 5,
        "end_to_end_ms": 10,
    }
    metrics = aggregate([{**row, "arm": "off"}, {**row, "arm": "on"}])
    tolerances = {
        "coverage_drop": 0,
        "correctness_drop": 0,
        "irrelevance_increase": 0,
        "latency_ratio": 1.2,
        "tokens_ratio": 1.2,
        "min_improvement": 0,
    }
    none_changed = {"total": 1, "evidence_changed": 0, "same_evidence_flips": 0}
    assert verdict(metrics, tolerances, none_changed)["verdict"] == "inconclusive"
    metrics["on"]["grounded_correct"] = 0
    changed = {**none_changed, "evidence_changed": 1}
    assert verdict(metrics, tolerances, changed)["verdict"] == "regression"


def test_live_runner_refuses_remote_databases(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.com/production")
    with pytest.raises(ValueError, match="loopback"):
        local_only()


def test_order_balances_first_arm_and_warmup():
    assert trial_order(-1) == (False, True)
    assert [trial_order(i) for i in range(4)] == [
        (False, True),
        (True, False),
        (False, True),
        (True, False),
    ]


def test_offline_accounting_includes_cached_and_failed_call_spend():
    phases = [
        SimpleNamespace(
            embedding_tokens=100,
            llm_breakdown=[
                SimpleNamespace(
                    input_tokens=200,
                    cached_input_tokens=30,
                    cache_write_tokens=20,
                    output_tokens=50,
                ),
            ],
        )
    ]
    usage = offline_usage(phases, {"embedding": 1, "input": 2, "output": 3})
    assert usage["input_tokens"] == 250
    assert usage["declared_usd"] == pytest.approx(0.00075)


def _config(tmp_path) -> dict:
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("{}", encoding="utf-8")
    return {
        "embedding_model": "local-embedding",
        "answer_model": "local-answer",
        "sleep_model": "local-judge",
        "model_revisions": {"embedding": "a", "answer": "b", "sleep": "c"},
        "tokenizer": str(tokenizer),
        "repetitions": 2,
        "k": 3,
        "max_chars": 6000,
        "tolerances": {
            "coverage_drop": 0,
            "correctness_drop": 0,
            "irrelevance_increase": 0,
            "latency_ratio": 1.1,
            "tokens_ratio": 1.1,
            "min_improvement": 0.05,
        },
        "usd_per_million": {"embedding": 0, "input": 0, "output": 0},
    }


def test_protocol_records_hashes_and_rejects_unbalanced_or_git_output(tmp_path):
    config = _config(tmp_path)
    manifest = validate_config(config, tmp_path / "output")
    assert len(manifest["tokenizer_sha256"]) == 64
    assert manifest["rollout"] is False
    with pytest.raises(ValueError, match="even"):
        validate_config({**config, "repetitions": 3}, tmp_path / "output")
    with pytest.raises(ValueError, match="tolerance"):
        validate_config(
            {**config, "tolerances": {**config["tolerances"], "coverage_drop": float("nan")}},
            tmp_path / "output",
        )
    (tmp_path / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    with pytest.raises(ValueError, match="outside every git checkout"):
        validate_config(config, tmp_path / "output")


def test_ingested_documents_never_carry_source_labels():
    """Tags reach the answer model with the evidence: labels must stay out."""
    documents, label_of = opaque_documents()
    assert sorted(label_of.values()) == sorted(SOURCES)
    for doc_id, text in documents:
        assert SOURCES[label_of[doc_id]] == text
        assert not any(label in doc_id for label in SOURCES)
        assert not any(value in doc_id for p in PROBES for value in p["sources"])


def _trial(arm, probe="p", repetition=0, evidence="e", extras=0, correct=1, coverage=1.0):
    return {
        "arm": arm,
        "probe": probe,
        "repetition": repetition,
        "evidence_sha256": evidence,
        "extras_in_evidence": extras,
        "grounded_correct": correct,
        "required_source_coverage": coverage,
    }


def test_pairs_credit_only_evidence_the_index_changed_and_count_flips():
    rows = [
        # Same evidence, different answer: answer-model noise, not the index.
        _trial("off", "a", evidence="x", correct=0),
        _trial("on", "a", evidence="x", correct=1),
        # The index put an extra into the on-arm evidence.
        _trial("off", "b", evidence="y"),
        _trial("on", "b", evidence="z", extras=1),
        # Added to the pool but cut before the page: no evidence change.
        _trial("off", "c", evidence="w"),
        _trial("on", "c", evidence="w", extras=0),
    ]
    assert pair_summary(rows) == {"total": 3, "evidence_changed": 1, "same_evidence_flips": 1}


def _metrics(**on_changes):
    base = {
        "required_source_coverage": 0.5,
        "irrelevant_evidence": 0.2,
        "grounded_correct": 0.5,
        "response_tokens": 100,
        "online_reported_tokens": 0,
        "answer_errors": 0,
        "retrieval_ms": {"p50": 5, "p95": 10},
        "end_to_end_ms": {"p50": 50, "p95": 100},
    }
    return {"off": dict(base), "on": {**base, **on_changes}}


TOLERANCES = {
    "coverage_drop": 0,
    "correctness_drop": 0,
    "irrelevance_increase": 0.05,
    "latency_ratio": 1.25,
    "tokens_ratio": 1.10,
    "min_improvement": 0.05,
}
CHANGED = {"total": 20, "evidence_changed": 10, "same_evidence_flips": 0}


def test_gain_must_beat_the_declared_minimum_plus_observed_noise():
    assert verdict(_metrics(grounded_correct=0.6), TOLERANCES, CHANGED)["verdict"] == (
        "promising-local-only"
    )
    assert verdict(_metrics(grounded_correct=0.54), TOLERANCES, CHANGED)["verdict"] == (
        "inconclusive"
    )
    noisy = {**CHANGED, "same_evidence_flips": 2}  # 10% flip rate
    assert verdict(_metrics(grounded_correct=0.6), TOLERANCES, noisy)["verdict"] == ("inconclusive")


def test_response_tokens_and_retrieval_latency_gate_without_provider_usage():
    tripled = verdict(_metrics(grounded_correct=0.9, response_tokens=300), TOLERANCES, CHANGED)
    assert tripled["verdict"] == "regression"
    assert tripled["provider_tokens_measured"] is False
    slow = _metrics(grounded_correct=0.9, retrieval_ms={"p50": 5, "p95": 50})
    assert verdict(slow, TOLERANCES, CHANGED)["verdict"] == "regression"


def test_more_answer_errors_on_the_on_arm_is_a_regression():
    assert verdict(_metrics(answer_errors=0.1), TOLERANCES, CHANGED)["verdict"] == "regression"


def test_non_object_answers_score_as_wrong_instead_of_crashing():
    probe = PROBES[1]
    for parsed in (None, [], "text"):
        assert score(probe, set(probe["sources"]), parsed, {})["grounded_correct"] == 0
        # ... and count as a failed answer, so the error-rate gate sees them.
        assert answer_problem(SimpleNamespace(parsed=parsed)) == "MalformedAnswer"
    assert answer_problem(SimpleNamespace(parsed={})) is None


def _loopback(monkeypatch):
    for key, value in {
        "DATABASE_URL": "postgresql+asyncpg://localhost/kmc_related_eval_test",
        "QDRANT_URL": "http://127.0.0.1:6333",
        "REDIS_URL": "redis://localhost:6379/0",
        "SELF_HOSTED_BASE_URL": "http://127.0.0.1:8000",
        "EMBEDDING_PROVIDER": "self_hosted",
        "ENABLE_NEURAL_MEMORY": "false",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("SUPERSEDE_VERIFY_MODE", "SLEEP_LLM_PROVIDER"):
        monkeypatch.delenv(key, raising=False)


def test_live_runner_refuses_a_write_path_judge_that_is_not_local(monkeypatch):
    _loopback(monkeypatch)
    local_only()
    monkeypatch.setenv("SUPERSEDE_VERIFY_MODE", "llm")
    local_only()  # no provider configured: the judge never runs
    monkeypatch.setenv("SLEEP_LLM_PROVIDER", "self_hosted")
    local_only()
    monkeypatch.setenv("SLEEP_LLM_PROVIDER", "openai")
    with pytest.raises(ValueError, match="SLEEP_LLM_PROVIDER"):
        local_only()
    monkeypatch.setenv("SUPERSEDE_VERIFY_MODE", "off")
    local_only()


def test_manifest_records_the_checkout_and_the_settings_that_shape_a_run(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_RELATED_INDEX_TIMEOUT_MS", "200")
    monkeypatch.setenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE", "0.8")
    monkeypatch.delenv("SUPERSEDE_VERIFY_MODE", raising=False)
    monkeypatch.setattr(runner, "git_state", lambda: {"git_commit": "c" * 40, "git_dirty": True})
    manifest = validate_config(_config(tmp_path), tmp_path / "output")
    assert manifest["git_commit"] == "c" * 40
    assert manifest["git_dirty"] is True
    assert manifest["recall"] == {"full": 3, "verbosity": "compact", "use_rerank": False}
    assert manifest["settings"]["RECALL_RELATED_INDEX_TIMEOUT_MS"] == "200"
    assert manifest["settings"]["SUPERSEDE_VERIFY_MODE"] == "off"
    assert manifest["settings"]["SUPERSEDE_VERIFY_MIN_CONFIDENCE"] == "0.8"


def test_git_state_reads_the_checkout_and_survives_a_missing_git(monkeypatch):
    replies = {"rev-parse": "a" * 40 + "\n", "status": " M backend/x.py\n"}

    def fake_run(command, **kwargs):
        return SimpleNamespace(stdout=replies[command[3]])

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert git_state() == {"git_commit": "a" * 40, "git_dirty": True}
    replies["status"] = ""
    assert git_state()["git_dirty"] is False

    def no_git(command, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert git_state() == {"git_commit": "unknown", "git_dirty": None}


def test_expansion_fallbacks_are_counted_and_still_logged():
    seen = []
    inner = SimpleNamespace(info=lambda event, **fields: seen.append(event), name="kept")
    usage = {"skips": []}
    log = SkipLog(inner, usage)
    log.info(SKIP_EVENT, error_type="TimeoutError")
    log.info("something_else", error_type="ignored")
    assert usage["skips"] == ["TimeoutError"]
    assert seen == [SKIP_EVENT, "something_else"]
    assert log.name == "kept"
    # The trial reset rebinds the list; the counter must follow it.
    usage.update(skips=[])
    log.info(SKIP_EVENT)
    assert usage["skips"] == ["unknown"]
    # The event name is the one the expansion logs when it falls back.
    source = Path(__file__).parents[2] / "src" / "services" / "recall_related_index.py"
    assert f'logger.info("{SKIP_EVENT}"' in source.read_text(encoding="utf-8")


def test_evidence_is_the_reply_the_mcp_recall_tool_sends():
    from mcp_server.tools import memory as mcp_memory
    from models.schemas import MemoryResponse, RecallResponse

    def memory(summary: str) -> MemoryResponse:
        return MemoryResponse(
            memory_id=uuid4(),
            summary=summary,
            context_summary="why it exists",
            type="note",
            importance=0.5,
            scope="persistent",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            updated_at=None,
            client="mcp",
            tags=["src-0123456789ab"],
            context=None,
            score=0.5,
            content="x" * 900,
        )

    context = SimpleNamespace(
        id=UUID("00000000-0000-4000-8000-000000000001"),
        name="eval",
        display_name="Eval",
        is_private=False,
        is_locked=False,
    )
    response = RecallResponse(results=[memory("first"), memory("second"), memory("third")])
    whole = mcp_evidence(response, context, 6000)
    assert whole == mcp_memory._bound_recall_envelope(
        mcp_memory._fit_recall_content(
            mcp_memory._recall_envelope(response, context, "compact"), 6000
        ),
        6000,
    )
    assert whole["status"] == "success" and whole["count"] == 3
    # The compact projection: no tags or context_summary, content on every row.
    assert all({"tags", "context_summary"}.isdisjoint(item) for item in whole["results"])
    assert all("content" in item for item in whole["results"])
    # Over budget the lowest-ranked rows lose their content first, as over MCP.
    tight = mcp_evidence(response, context, 2000)
    assert tight["full_applied"] < 3
    assert "content" not in tight["results"][-1]
