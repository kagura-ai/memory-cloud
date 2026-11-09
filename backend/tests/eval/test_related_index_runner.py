"""Protocol checks only: these tests do not make live quality claims."""

from types import SimpleNamespace

import pytest

from tests.eval.related_index_cases import PROBES, SOURCES
from tests.eval.related_index_runner import (
    aggregate,
    local_only,
    offline_usage,
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
    }
    assert verdict(metrics, tolerances, 0) == "inconclusive"
    metrics["on"]["grounded_correct"] = 0
    assert verdict(metrics, tolerances, 1) == "regression"


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


def test_protocol_records_hashes_and_rejects_unbalanced_or_git_output(tmp_path):
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("{}", encoding="utf-8")
    config = {
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
        },
        "usd_per_million": {"embedding": 0, "input": 0, "output": 0},
    }
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
