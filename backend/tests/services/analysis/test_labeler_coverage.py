"""Coverage tests for Stage [F + G]: representative selection + LLM labeling.

Covers ``services.analysis.labeler``:

* ``_select_representatives`` (PURE) — small-cluster passthrough, top-k
  centroid-distance selection, ordering, zero-norm safety, k boundary.
* ``_format_representatives`` (PURE) — bullet rendering, None-summary
  fallback, newline flattening, 240-char truncation.
* ``_empty_cluster_label`` — sentinel shape.
* ``_label_one_cluster`` — happy path, confidence clamping/coercion,
  hallucination filtering, locale routing, upstream-error fallback.
  The LLM ``call_with_fallback`` is monkeypatched so no network/LLM
  call occurs; the per-task ``AsyncSession`` opens against the
  throwaway QA DB.
* ``label_clusters`` — parallel labeling, empty-cluster sentinels,
  ordering by ``cluster_index``, locale passthrough.

No real LLM or network call is made. ``call_with_fallback`` and the
upstream-error type are patched at the ``labeler`` module boundary.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import numpy as np
import pytest

from services.analysis import labeler
from services.analysis.labeler import (
    ClusterLabel,
    _empty_cluster_label,
    _format_representatives,
    _label_one_cluster,
    _select_representatives,
    label_clusters,
)
from services.analysis.llm_caller import (
    AnalysisLLMUpstreamError,
    CallResult,
)
from services.analysis.vector_pull import MemoryRecord
from services.llm_service import LLMResponse


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _mem(
    *,
    summary: str = "a summary",
    type_: str = "pattern",
    mem_id=None,
) -> MemoryRecord:
    """Build a MemoryRecord with a unique id unless one is supplied."""
    return MemoryRecord(
        id=mem_id or uuid4(),
        type=type_,
        summary=summary,
        tags=["t"],
        importance=0.5,
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )


def _llm_response(
    *,
    parsed: dict,
    model: str = "gpt-5-nano",
    provider: str = "openai",
) -> LLMResponse:
    """A minimal LLMResponse the breakdown accumulator can consume."""
    return LLMResponse(
        parsed=parsed,
        total_tokens=30,
        input_tokens=20,
        output_tokens=10,
        cached_input_tokens=0,
        provider=provider,
        model=model,
        cache_write_tokens=0,
        tokenizer_version="tok-v1",
    )


def _patch_caller(monkeypatch, *, parsed: dict, captured: dict | None = None) -> None:
    """Patch labeler.call_with_fallback to return a canned CallResult."""

    async def _fake_call_with_fallback(
        *, llm_service, user_id, workspace_id, context_id, system_prompt, prompt, fallback_chain
    ):  # noqa: ANN001, E501
        if captured is not None:
            captured["system_prompt"] = system_prompt
            captured["prompt"] = prompt
            captured["user_id"] = user_id
            captured["workspace_id"] = workspace_id
            captured["context_id"] = context_id
        resp = _llm_response(parsed=parsed)
        return CallResult(parsed=resp.parsed, response=resp)

    monkeypatch.setattr(labeler, "call_with_fallback", _fake_call_with_fallback)


def _patch_caller_raises(monkeypatch) -> None:
    """Patch labeler.call_with_fallback to raise the upstream 502 error."""

    async def _raise(**_kwargs):  # noqa: ANN003
        raise AnalysisLLMUpstreamError(
            "all models down", attempted_models=["gpt-5-nano", "gpt-5.5"]
        )

    monkeypatch.setattr(labeler, "call_with_fallback", _raise)


# --------------------------------------------------------------------------- #
# _select_representatives (pure)
# --------------------------------------------------------------------------- #
class TestSelectRepresentatives:
    """Stage [F]: top-k closest-to-centroid selection."""

    def test_small_cluster_returns_all_members_in_order(self) -> None:
        """When members <= k, all members are returned (no distance math)."""
        memories = [_mem(summary=f"m{i}") for i in range(4)]
        member_idx = np.array([0, 2, 3], dtype=np.int64)
        embeddings = np.random.default_rng(1).normal(size=(4, 8)).astype(np.float32)
        centroid = np.ones(8, dtype=np.float32)

        reps = _select_representatives(centroid, member_idx, embeddings, memories, k=5)

        assert [r.summary for r in reps] == ["m0", "m2", "m3"]

    def test_exactly_k_members_returns_all(self) -> None:
        """Boundary: len == k hits the passthrough branch (<=)."""
        memories = [_mem(summary=f"m{i}") for i in range(5)]
        member_idx = np.array([0, 1, 2, 3, 4], dtype=np.int64)
        embeddings = np.random.default_rng(2).normal(size=(5, 8)).astype(np.float32)
        centroid = np.ones(8, dtype=np.float32)

        reps = _select_representatives(centroid, member_idx, embeddings, memories, k=5)

        assert len(reps) == 5
        assert {r.summary for r in reps} == {f"m{i}" for i in range(5)}

    def test_selects_k_closest_to_centroid(self) -> None:
        """More than k members: the k nearest to the centroid are picked.

        Construct embeddings so member 3 and 4 point along the centroid
        direction (closest) and 0,1,2 point away. With k=2 the result
        must be exactly {m3, m4}.
        """
        # centroid along +x axis
        centroid = np.array([1.0, 0.0], dtype=np.float32)
        embeddings = np.array(
            [
                [-1.0, 0.0],  # idx0 opposite -> far
                [0.0, 1.0],  # idx1 orthogonal
                [0.0, -1.0],  # idx2 orthogonal
                [1.0, 0.0],  # idx3 aligned -> closest
                [0.9, 0.1],  # idx4 nearly aligned -> close
            ],
            dtype=np.float32,
        )
        memories = [_mem(summary=f"m{i}") for i in range(5)]
        member_idx = np.array([0, 1, 2, 3, 4], dtype=np.int64)

        reps = _select_representatives(centroid, member_idx, embeddings, memories, k=2)

        assert len(reps) == 2
        assert {r.summary for r in reps} == {"m3", "m4"}

    def test_respects_member_index_mapping(self) -> None:
        """Chosen reps map back through cluster_member_indices, not raw rows."""
        centroid = np.array([1.0, 0.0], dtype=np.float32)
        embeddings = np.array(
            [
                [1.0, 0.0],  # idx0 aligned but NOT a member
                [-1.0, 0.0],  # idx1 member, far
                [0.95, 0.05],  # idx2 member, close
                [0.9, 0.1],  # idx3 member, close
            ],
            dtype=np.float32,
        )
        memories = [_mem(summary=f"m{i}") for i in range(4)]
        # Only 1,2,3 are members; idx0 (most aligned) must be excluded.
        member_idx = np.array([1, 2, 3], dtype=np.int64)

        reps = _select_representatives(centroid, member_idx, embeddings, memories, k=2)

        assert {r.summary for r in reps} == {"m2", "m3"}
        assert "m0" not in {r.summary for r in reps}

    def test_zero_norm_centroid_does_not_crash(self) -> None:
        """A zero centroid is normalized via the 1e-12 floor (no div-by-zero)."""
        centroid = np.zeros(4, dtype=np.float32)
        embeddings = np.random.default_rng(3).normal(size=(6, 4)).astype(np.float32)
        memories = [_mem(summary=f"m{i}") for i in range(6)]
        member_idx = np.array([0, 1, 2, 3, 4, 5], dtype=np.int64)

        reps = _select_representatives(centroid, member_idx, embeddings, memories, k=2)

        # Selection still returns exactly k; no NaN explosion / exception.
        assert len(reps) == 2

    def test_zero_norm_row_does_not_crash(self) -> None:
        """A zero-vector member row is normalized via the per-row floor."""
        centroid = np.array([1.0, 0.0], dtype=np.float32)
        embeddings = np.array(
            [
                [0.0, 0.0],  # zero row
                [1.0, 0.0],
                [0.5, 0.5],
                [0.2, 0.8],
            ],
            dtype=np.float32,
        )
        memories = [_mem(summary=f"m{i}") for i in range(4)]
        member_idx = np.array([0, 1, 2, 3], dtype=np.int64)

        reps = _select_representatives(centroid, member_idx, embeddings, memories, k=2)

        assert len(reps) == 2


# --------------------------------------------------------------------------- #
# _format_representatives (pure)
# --------------------------------------------------------------------------- #
class TestFormatRepresentatives:
    """Render the rep list as the user-prompt body (Layer-1 only)."""

    def test_renders_type_and_summary_bullets(self) -> None:
        """Each rep becomes a ``- [type] summary`` line."""
        reps = [
            _mem(summary="alpha", type_="pattern"),
            _mem(summary="beta", type_="fact"),
        ]
        out = _format_representatives(reps)
        assert out == "- [pattern] alpha\n- [fact] beta"

    def test_none_summary_falls_back_to_placeholder(self) -> None:
        """A None summary renders as ``(no summary)``."""
        reps = [_mem(type_="note")]
        # Bypass dataclass type hint to simulate a null summary.
        object.__setattr__(reps[0], "summary", None)
        out = _format_representatives(reps)
        assert out == "- [note] (no summary)"

    def test_newlines_are_flattened_to_spaces(self) -> None:
        """Embedded newlines are replaced so each rep stays one line."""
        reps = [_mem(summary="line1\nline2\nline3", type_="x")]
        out = _format_representatives(reps)
        assert out == "- [x] line1 line2 line3"
        assert "\n" not in out.split("] ", 1)[1]

    def test_summary_truncated_to_240_chars(self) -> None:
        """Long summaries are clipped to 240 characters."""
        long_summary = "z" * 500
        reps = [_mem(summary=long_summary, type_="x")]
        out = _format_representatives(reps)
        body = out.split("] ", 1)[1]
        assert len(body) == 240
        assert body == "z" * 240

    def test_empty_rep_list_returns_empty_string(self) -> None:
        """No reps => empty joined string."""
        assert _format_representatives([]) == ""

    def test_whitespace_summary_is_stripped(self) -> None:
        """Leading/trailing whitespace is stripped before truncation."""
        reps = [_mem(summary="   hello   ", type_="x")]
        out = _format_representatives(reps)
        assert out == "- [x] hello"


# --------------------------------------------------------------------------- #
# _empty_cluster_label
# --------------------------------------------------------------------------- #
class TestEmptyClusterLabel:
    """Sentinel produced for empty clusters."""

    async def test_returns_empty_sentinel(self) -> None:
        """Empty cluster sentinel carries the documented fixed values."""
        cl = await _empty_cluster_label(7)
        assert cl.cluster_index == 7
        assert cl.label == "(empty)"
        assert cl.description == "No memories were assigned to this cluster."
        assert cl.label_confidence == 0.0
        assert cl.representative_memory_ids == []
        assert cl.breakdown is None
        assert cl.failed is False


# --------------------------------------------------------------------------- #
# _label_one_cluster
# --------------------------------------------------------------------------- #
class TestLabelOneCluster:
    """Single-cluster labeling with semaphore + frozenset guard."""

    async def test_happy_path_builds_cluster_label(self, monkeypatch) -> None:
        """Parsed label/description/confidence flow into the ClusterLabel."""
        import asyncio

        reps = [_mem(summary="m0"), _mem(summary="m1")]
        rep_ids = [str(r.id) for r in reps]
        captured: dict = {}
        _patch_caller(
            monkeypatch,
            parsed={
                "label": "  Cloud Infra  ",
                "description": "  notes about infra  ",
                "label_confidence": 0.73,
            },
            captured=captured,
        )

        cl = await _label_one_cluster(
            cluster_index=2,
            reps=reps,
            user_id="u1",
            workspace_id="w1",
            context_id="c1",
            sem=asyncio.Semaphore(1),
        )

        assert cl.cluster_index == 2
        assert cl.label == "Cloud Infra"  # stripped
        assert cl.description == "notes about infra"  # stripped
        assert cl.label_confidence == pytest.approx(0.73)
        # No hallucinated ids in response -> falls back to labeler's reps.
        assert cl.representative_memory_ids == rep_ids
        assert cl.failed is False
        assert cl.breakdown is not None
        assert cl.breakdown.provider == "openai"
        assert cl.breakdown.calls == 1
        # en locale selects the English prompts (no JA marker).
        assert captured["user_id"] == "u1"
        assert captured["context_id"] == "c1"

    async def test_confidence_clamped_above_one(self, monkeypatch) -> None:
        """A confidence > 1 is clamped to 1.0."""
        import asyncio

        _patch_caller(monkeypatch, parsed={"label": "x", "label_confidence": 5.0})
        cl = await _label_one_cluster(
            cluster_index=0,
            reps=[_mem()],
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
        )
        assert cl.label_confidence == 1.0

    async def test_confidence_clamped_below_zero(self, monkeypatch) -> None:
        """A negative confidence is clamped to 0.0."""
        import asyncio

        _patch_caller(monkeypatch, parsed={"label": "x", "label_confidence": -3.0})
        cl = await _label_one_cluster(
            cluster_index=0,
            reps=[_mem()],
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
        )
        assert cl.label_confidence == 0.0

    async def test_confidence_non_numeric_defaults_to_zero(self, monkeypatch) -> None:
        """A non-coercible confidence falls back to 0.0 via except branch."""
        import asyncio

        _patch_caller(monkeypatch, parsed={"label": "x", "label_confidence": "not-a-number"})
        cl = await _label_one_cluster(
            cluster_index=0,
            reps=[_mem()],
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
        )
        assert cl.label_confidence == 0.0

    async def test_missing_label_uses_unlabeled_sentinel(self, monkeypatch) -> None:
        """An absent label key defaults to ``(unlabeled)``."""
        import asyncio

        _patch_caller(monkeypatch, parsed={})
        cl = await _label_one_cluster(
            cluster_index=0,
            reps=[_mem()],
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
        )
        assert cl.label == "(unlabeled)"
        assert cl.description == ""
        assert cl.label_confidence == 0.0

    async def test_blank_label_falls_back_to_unlabeled(self, monkeypatch) -> None:
        """A whitespace-only label becomes ``(unlabeled)`` (or-fallback branch)."""
        import asyncio

        _patch_caller(monkeypatch, parsed={"label": "   ", "label_confidence": 0.4})
        cl = await _label_one_cluster(
            cluster_index=0,
            reps=[_mem()],
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
        )
        assert cl.label == "(unlabeled)"

    async def test_hallucinated_ids_filtered_to_requested_set(self, monkeypatch) -> None:
        """Returned ids outside the rep set are dropped; in-set ones kept."""
        import asyncio

        reps = [_mem(summary="m0"), _mem(summary="m1")]
        rep_ids = [str(r.id) for r in reps]
        # LLM returns one valid id + one invented id.
        _patch_caller(
            monkeypatch,
            parsed={
                "label": "x",
                "representative_memory_ids": [rep_ids[0], "deadbeef-invented"],
            },
        )
        cl = await _label_one_cluster(
            cluster_index=0,
            reps=reps,
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
        )
        # Only the valid id survives; invented one is filtered out.
        assert cl.representative_memory_ids == [rep_ids[0]]

    async def test_all_hallucinated_ids_falls_back_to_reps(self, monkeypatch) -> None:
        """If filtering empties the list, the labeler's own reps are used."""
        import asyncio

        reps = [_mem(summary="m0"), _mem(summary="m1")]
        rep_ids = [str(r.id) for r in reps]
        _patch_caller(
            monkeypatch,
            parsed={"label": "x", "representative_memory_ids": ["nope-1", "nope-2"]},
        )
        cl = await _label_one_cluster(
            cluster_index=0,
            reps=reps,
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
        )
        assert cl.representative_memory_ids == rep_ids

    async def test_ja_locale_selects_japanese_prompts(self, monkeypatch) -> None:
        """locale='ja' routes through the JA prompt pair."""
        import asyncio

        from services.analysis.prompts import CLUSTER_LABEL_SYSTEM_JA

        captured: dict = {}
        _patch_caller(monkeypatch, parsed={"label": "x"}, captured=captured)
        await _label_one_cluster(
            cluster_index=0,
            reps=[_mem()],
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
            locale="ja",
        )
        assert captured["system_prompt"] == CLUSTER_LABEL_SYSTEM_JA

    async def test_unknown_locale_falls_back_to_english(self, monkeypatch) -> None:
        """An unmapped locale uses the English prompt pair (default branch)."""
        import asyncio

        from services.analysis.prompts import CLUSTER_LABEL_SYSTEM

        captured: dict = {}
        _patch_caller(monkeypatch, parsed={"label": "x"}, captured=captured)
        await _label_one_cluster(
            cluster_index=0,
            reps=[_mem()],
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
            locale="fr",
        )
        assert captured["system_prompt"] == CLUSTER_LABEL_SYSTEM

    async def test_rep_block_substituted_into_prompt(self, monkeypatch) -> None:
        """The rendered rep block is substituted into the user template."""
        import asyncio

        captured: dict = {}
        reps = [_mem(summary="uniquemarker123", type_="pattern")]
        _patch_caller(monkeypatch, parsed={"label": "x"}, captured=captured)
        await _label_one_cluster(
            cluster_index=0,
            reps=reps,
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
        )
        assert "uniquemarker123" in captured["prompt"]
        assert "- [pattern] uniquemarker123" in captured["prompt"]

    async def test_upstream_error_returns_failed_label(self, monkeypatch) -> None:
        """When the fallback chain is exhausted, a failed sentinel is returned."""
        import asyncio

        reps = [_mem(summary="m0"), _mem(summary="m1")]
        rep_ids = [str(r.id) for r in reps]
        _patch_caller_raises(monkeypatch)

        cl = await _label_one_cluster(
            cluster_index=4,
            reps=reps,
            user_id="u",
            workspace_id="w",
            context_id=None,
            sem=asyncio.Semaphore(1),
        )

        assert cl.cluster_index == 4
        assert cl.failed is True
        assert cl.label == "(unlabeled)"
        assert cl.description == "LLM labeling failed for this cluster."
        assert cl.label_confidence == 0.0
        # On failure the rep ids are still recorded (from the labeler's reps).
        assert cl.representative_memory_ids == rep_ids
        assert cl.breakdown is None


# --------------------------------------------------------------------------- #
# label_clusters
# --------------------------------------------------------------------------- #
class TestLabelClusters:
    """Parallel labeling over all clusters (semaphore-bounded)."""

    async def test_labels_all_clusters_ordered_by_index(self, monkeypatch) -> None:
        """Two non-empty clusters produce ordered, labeled results."""
        # 4 memories: rows 0,2 -> cluster 0; rows 1,3 -> cluster 1.
        memories = [_mem(summary=f"m{i}") for i in range(4)]
        cluster_labels = np.array([0, 1, 0, 1], dtype=np.int64)
        centroids = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        embeddings = np.array(
            [[1.0, 0.0], [0.0, 1.0], [0.9, 0.1], [0.1, 0.9]],
            dtype=np.float32,
        )
        _patch_caller(monkeypatch, parsed={"label": "L", "label_confidence": 0.5})

        results = await label_clusters(
            cluster_labels=cluster_labels,
            centroids=centroids,
            embeddings=embeddings,
            memories=memories,
            user_id="u",
            workspace_id="w",
            context_id=None,
        )

        assert [r.cluster_index for r in results] == [0, 1]
        assert all(r.label == "L" for r in results)
        assert all(not r.failed for r in results)

    async def test_empty_cluster_gets_sentinel(self, monkeypatch) -> None:
        """A cluster with no members yields the empty sentinel, not an LLM call.

        centroids has 3 clusters but cluster_labels only references 0 and 2,
        so cluster 1 is empty.
        """
        memories = [_mem(summary=f"m{i}") for i in range(2)]
        cluster_labels = np.array([0, 2], dtype=np.int64)
        centroids = np.array([[1.0, 0.0], [0.0, 1.0], [0.0, -1.0]], dtype=np.float32)
        embeddings = np.array([[1.0, 0.0], [0.0, -1.0]], dtype=np.float32)

        called = {"n": 0}

        async def _counting(**kwargs):  # noqa: ANN003
            called["n"] += 1
            resp = _llm_response(parsed={"label": "L"})
            return CallResult(parsed=resp.parsed, response=resp)

        monkeypatch.setattr(labeler, "call_with_fallback", _counting)

        results = await label_clusters(
            cluster_labels=cluster_labels,
            centroids=centroids,
            embeddings=embeddings,
            memories=memories,
            user_id="u",
            workspace_id="w",
            context_id=None,
        )

        assert [r.cluster_index for r in results] == [0, 1, 2]
        # cluster 1 is the empty sentinel.
        assert results[1].label == "(empty)"
        assert results[1].representative_memory_ids == []
        # Only the two non-empty clusters triggered an LLM call.
        assert called["n"] == 2

    async def test_failed_clusters_are_counted_but_returned(self, monkeypatch) -> None:
        """When the LLM chain is exhausted, failed labels are still returned."""
        memories = [_mem(summary=f"m{i}") for i in range(2)]
        cluster_labels = np.array([0, 1], dtype=np.int64)
        centroids = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        embeddings = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        _patch_caller_raises(monkeypatch)

        results = await label_clusters(
            cluster_labels=cluster_labels,
            centroids=centroids,
            embeddings=embeddings,
            memories=memories,
            user_id="u",
            workspace_id="w",
            context_id=None,
        )

        assert len(results) == 2
        assert all(r.failed for r in results)
        assert all(r.label == "(unlabeled)" for r in results)

    async def test_locale_is_passed_through_to_tasks(self, monkeypatch) -> None:
        """locale flows from label_clusters into _label_one_cluster prompts."""
        from services.analysis.prompts import CLUSTER_LABEL_SYSTEM_JA

        memories = [_mem(summary="m0")]
        cluster_labels = np.array([0], dtype=np.int64)
        centroids = np.array([[1.0, 0.0]], dtype=np.float32)
        embeddings = np.array([[1.0, 0.0]], dtype=np.float32)

        seen: dict = {}

        async def _capture(**kwargs):  # noqa: ANN003
            seen["system_prompt"] = kwargs["system_prompt"]
            resp = _llm_response(parsed={"label": "L"})
            return CallResult(parsed=resp.parsed, response=resp)

        monkeypatch.setattr(labeler, "call_with_fallback", _capture)

        await label_clusters(
            cluster_labels=cluster_labels,
            centroids=centroids,
            embeddings=embeddings,
            memories=memories,
            user_id="u",
            workspace_id="w",
            context_id=None,
            locale="ja",
        )

        assert seen["system_prompt"] == CLUSTER_LABEL_SYSTEM_JA

    async def test_all_clusters_empty_when_no_memories(self, monkeypatch) -> None:
        """Centroids present but zero memories => every cluster is a sentinel."""
        cluster_labels = np.array([], dtype=np.int64)
        centroids = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        embeddings = np.zeros((0, 2), dtype=np.float32)

        called = {"n": 0}

        async def _counting(**kwargs):  # noqa: ANN003
            called["n"] += 1
            resp = _llm_response(parsed={"label": "L"})
            return CallResult(parsed=resp.parsed, response=resp)

        monkeypatch.setattr(labeler, "call_with_fallback", _counting)

        results = await label_clusters(
            cluster_labels=cluster_labels,
            centroids=centroids,
            embeddings=embeddings,
            memories=[],
            user_id="u",
            workspace_id="w",
            context_id=None,
        )

        assert [r.cluster_index for r in results] == [0, 1]
        assert all(r.label == "(empty)" for r in results)
        assert called["n"] == 0


# --------------------------------------------------------------------------- #
# ClusterLabel dataclass
# --------------------------------------------------------------------------- #
class TestClusterLabelDataclass:
    """The frozen output dataclass."""

    def test_failed_defaults_to_false(self) -> None:
        """``failed`` is an optional field defaulting to False."""
        cl = ClusterLabel(
            cluster_index=0,
            label="x",
            description="d",
            label_confidence=0.5,
            representative_memory_ids=["a"],
            breakdown=None,
        )
        assert cl.failed is False

    def test_is_frozen(self) -> None:
        """ClusterLabel is immutable (frozen dataclass)."""
        cl = ClusterLabel(
            cluster_index=0,
            label="x",
            description="d",
            label_confidence=0.5,
            representative_memory_ids=[],
            breakdown=None,
        )
        with pytest.raises(AttributeError):
            cl.label = "y"  # type: ignore[misc]
