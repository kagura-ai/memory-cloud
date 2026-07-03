"""Unit tests for the Day-4 static-runner parameterization (Issue #344 follow-up).

Pure-function tests only — no DB/stack. ``tests.eval.runner`` keeps its heavy
(DB/services) imports function-local specifically so this module (and the
runner module itself) stays importable in plain CI; these tests exercise that
by never touching anything beyond the pure helpers plus one guarded call into
``run_retrieval_eval`` that must fail *before* any stack access.

Stub queries are ``types.SimpleNamespace`` objects carrying only the
``.id`` / ``.bucket`` / ``.split`` / ``.relevant`` attributes the helpers
under test actually read — the real ``Query`` dataclass lives in
``tests.eval.tools.corpus`` and pulls in the YAML loader, which is irrelevant
here.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from tests.eval import runner


def _q(id_: str, bucket: str, split: str | None, relevant: list[str]):
    return types.SimpleNamespace(id=id_, bucket=bucket, split=split, relevant=relevant)


class TestPerQueryRecords:
    def test_hand_computable_metrics_and_split_passthrough(self):
        queries = [
            _q("q1", "retrieval-exact", "heldout", ["a"]),
            _q("q2", "cross-source", "public", ["x", "y"]),
            _q("q3", "memory-only", None, ["z"]),
        ]
        rankings = [
            (["a", "b", "c", "d", "e"], {"a"}),  # p@5: 1/5 hit; rr@10: rank 1 -> 1.0
            (["x2", "y", "b", "c", "d"], {"x", "y"}),  # p@5: 1/5 hit; rr@10: rank 2 -> 0.5
            (["m", "n"], {"z"}),  # p@5: 0 hits; rr@10: no hit -> 0.0
        ]

        records = runner._per_query_records(queries, rankings)

        assert records == [
            {
                "query_id": "q1",
                "bucket": "retrieval-exact",
                "split": "heldout",
                "p@5": 0.2,
                "rr@10": 1.0,
            },
            {
                "query_id": "q2",
                "bucket": "cross-source",
                "split": "public",
                "p@5": 0.2,
                "rr@10": 0.5,
            },
            {
                "query_id": "q3",
                "bucket": "memory-only",
                "split": None,
                "p@5": 0.0,
                "rr@10": 0.0,
            },
        ]

    def test_corpus_order_is_preserved_not_reordered(self):
        # Alternating hit/miss so any accidental sort/group-by would scramble it.
        queries = [_q(f"q{i}", "b", None, ["x"]) for i in range(5)]
        rankings = [(["x"], {"x"}) if i % 2 == 0 else (["y"], {"x"}) for i in range(5)]

        records = runner._per_query_records(queries, rankings)

        assert [r["query_id"] for r in records] == [f"q{i}" for i in range(5)]
        assert [r["rr@10"] for r in records] == [1.0, 0.0, 1.0, 0.0, 1.0]

    def test_length_mismatch_raises_value_error(self):
        queries = [_q("q1", "b", None, ["a"])]
        rankings: list[tuple[list[str], set[str]]] = []

        with pytest.raises(ValueError, match="length"):
            runner._per_query_records(queries, rankings)


class TestSplitMetrics:
    def test_all_none_split_returns_none(self):
        # Golden corpus shape: no query carries a split.
        queries = [_q(f"q{i}", "b", None, ["a"]) for i in range(3)]
        rankings = [(["a"], {"a"}) for _ in queries]

        assert runner._split_metrics(queries, rankings) is None

    def test_mixed_splits_only_present_ones_with_matching_n(self):
        queries = [
            _q("q1", "b", "heldout", ["a"]),
            _q("q2", "b", "heldout", ["a"]),
            _q("q3", "b", "public", ["a"]),
            _q("q4", "b", None, ["a"]),
        ]
        rankings = [
            (["a"], {"a"}),
            (["x"], {"a"}),
            (["a"], {"a"}),
            (["a"], {"a"}),
        ]

        result = runner._split_metrics(queries, rankings)

        assert result is not None
        assert set(result) == {"heldout", "public"}
        assert result["heldout"]["n"] == 2
        assert result["public"]["n"] == 1

    def test_none_split_query_excluded_from_every_block(self):
        queries = [
            _q("q1", "b", "heldout", ["a"]),
            _q("q2", "b", None, ["a"]),
        ]
        rankings = [
            (["a"], {"a"}),
            (["zzz"], {"a"}),  # would tank metrics if ever counted anywhere
        ]

        result = runner._split_metrics(queries, rankings)

        assert result is not None
        assert set(result) == {"heldout"}
        assert result["heldout"]["n"] == 1


class TestResultsFilename:
    def test_no_label_keeps_legacy_filename(self):
        assert runner._results_filename(None, "2026-07-03") == "2026-07-03.json"

    def test_label_prefixes_filename(self):
        assert (
            runner._results_filename("day4-x-run0", "2026-07-03") == "day4-x-run0-2026-07-03.json"
        )


class TestEmbeddingModelValidation:
    def test_unknown_model_raises_before_touching_db(self):
        """Registry check must happen before ``async for db in get_db()`` so this
        test needs no live stack — an unavailable DB would raise a different
        error (or hang), not ``ValueError``.
        """
        from config.constants import EMBEDDING_MODEL_REGISTRY

        with pytest.raises(ValueError) as exc_info:
            asyncio.run(runner.run_retrieval_eval(embedding_model="nope"))

        message = str(exc_info.value)
        assert "nope" in message
        for key in EMBEDDING_MODEL_REGISTRY:
            assert key in message
