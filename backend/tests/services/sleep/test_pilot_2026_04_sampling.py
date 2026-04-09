"""Determinism + allocation tests for the pilot #249 sampling script.

The sampling script lives at:
    backend/tests/services/sleep/eval/pilot_2026_04/sampling_script.py

It is a CLI script, not a package — there is intentionally no __init__.py
inside the pilot dir so pytest does not treat it as a test package. We load
it via importlib so this test can verify its constants and pure helpers
without spinning up a DB.

These tests live OUTSIDE the pilot dir (one level up at
backend/tests/services/sleep/) so pytest collects them normally without
inadvertently picking up any test_*.py files inside the pilot dir.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import UUID

import numpy as np
import pytest

PILOT_DIR = Path(__file__).parent / "eval" / "pilot_2026_04"
SAMPLING_SCRIPT = PILOT_DIR / "sampling_script.py"


@pytest.fixture(scope="module")
def sampling_module():
    """Load sampling_script.py via importlib (it is not a normal package)."""
    spec = importlib.util.spec_from_file_location("pilot_2026_04_sampling", SAMPLING_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pilot_2026_04_sampling"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_memory(mod, idx: int, vector: np.ndarray, tags: tuple[str, ...]):
    """Build a MemoryRecord from a fixture vector (unit-normalized)."""
    norm = float(np.linalg.norm(vector))
    return mod.MemoryRecord(
        id=UUID(f"00000000-0000-0000-0000-{idx:012x}"),
        summary=f"memory {idx}",
        tags=tags,
        vector=vector / norm,
        created_at_iso="2026-04-09T00:00:00+00:00",
    )


def _fake_memories(mod, n: int, seed: int) -> list:
    """n unit-norm 8-dim memories with deterministic tags."""
    rng = np.random.default_rng(seed)
    vecs = rng.standard_normal((n, 8))
    return [_make_memory(mod, i, vecs[i], tags=(f"t{i % 3}", f"t{i % 5}")) for i in range(n)]


# ----------------------------------------------------------------------------
# Refinement #1: per-cell allocation correctness
# ----------------------------------------------------------------------------


def test_allocation_constants_sum_to_50(sampling_module):
    """ALLOCATION must total exactly 50 pairs across 2 contexts × 4 strata."""
    alloc = sampling_module.ALLOCATION
    total = sum(cell for ctx in alloc.values() for cell in ctx.values())
    assert total == 50, f"ALLOCATION sums to {total}, expected 50"
    assert sum(alloc["kagura-dev"].values()) == 30
    assert sum(alloc["personal_memo"].values()) == 20

    # Per-stratum totals match the README table.
    assert alloc["kagura-dev"]["A"] + alloc["personal_memo"]["A"] == 25
    assert alloc["kagura-dev"]["B"] + alloc["personal_memo"]["B"] == 10
    assert alloc["kagura-dev"]["C"] + alloc["personal_memo"]["C"] == 8
    assert alloc["kagura-dev"]["D"] + alloc["personal_memo"]["D"] == 7


def test_fallback_allocation_sums_to_50(sampling_module):
    """ALLOCATION_FALLBACK puts all 50 pairs in kagura-dev."""
    alloc = sampling_module.ALLOCATION_FALLBACK
    total = sum(cell for ctx in alloc.values() for cell in ctx.values())
    assert total == 50
    assert sum(alloc["kagura-dev"].values()) == 50
    assert sum(alloc["personal_memo"].values()) == 0
    assert alloc["kagura-dev"]["A"] == 25
    assert alloc["kagura-dev"]["B"] == 10
    assert alloc["kagura-dev"]["C"] == 8
    assert alloc["kagura-dev"]["D"] == 7


# ----------------------------------------------------------------------------
# Production parity
# ----------------------------------------------------------------------------


def test_synthetic_seed_matches_production(sampling_module):
    """The pilot's is_synthetic_seed must mirror production EXACTLY.

    If services/sleep/edge_discovery._is_synthetic_seed_edge drifts in a
    future #248 follow-up, this test fires loud and the pilot's "no
    existing edge" Stratum A filter is no longer measuring what production
    measures. That's a critical correctness invariant for the probe.
    """
    from services.sleep.edge_discovery import _is_synthetic_seed_edge  # type: ignore[import-not-found]

    class FakeEdge:
        def __init__(self, et: str, w: float):
            self.edge_type = et
            self.weight = w

    cases = [
        ("semantic_similarity", 0.30, True),  # below threshold → synthetic
        ("semantic_similarity", 0.49, True),
        ("semantic_similarity", 0.50, False),  # boundary
        ("semantic_similarity", 0.70, False),
        ("related_to", 0.10, False),  # any non-semantic_similarity is real
        ("depends_on", 0.49, False),
        ("learned_from", 0.10, False),
        ("neural_association", 0.30, False),
    ]
    for et, w, expected in cases:
        prod = _is_synthetic_seed_edge(FakeEdge(et, w))
        pilot = sampling_module.is_synthetic_seed(et, w)
        assert prod == expected, f"production drifted for ({et}, {w})"
        assert pilot == expected, f"pilot drifted for ({et}, {w})"
        assert prod == pilot, f"production-pilot mismatch for ({et}, {w})"


# ----------------------------------------------------------------------------
# Determinism
# ----------------------------------------------------------------------------


def test_sample_stratum_is_deterministic(sampling_module):
    """Two calls with the same seed and same input → identical output."""
    mod = sampling_module
    mems = _fake_memories(mod, n=20, seed=1)
    cosine = mod.compute_cosine_matrix(mems)
    candidates = mod.build_eligible_pairs(
        mems,
        cosine,
        edges_by_pair={},
        band=(-1.0, 1.0),
        require_no_edge=False,
        half_open=False,
    )
    rng1 = np.random.default_rng(42)
    rng2 = np.random.default_rng(42)
    out1 = mod.sample_stratum(candidates, n=5, rng=rng1)
    out2 = mod.sample_stratum(candidates, n=5, rng=rng2)
    assert len(out1) == 5
    assert [c.canonical_key for c in out1] == [c.canonical_key for c in out2]


def test_sample_stratum_is_reorder_invariant(sampling_module):
    """Loading memories in a different order must NOT change the seed=42
    sample. This is the test that catches dict/set iteration nondeterminism.

    build_eligible_pairs sorts by canonical_key BEFORE the RNG sees it, so
    the input order should be irrelevant once compute_cosine_matrix has run.
    """
    mod = sampling_module
    mems = _fake_memories(mod, n=20, seed=1)
    mems_reversed = list(reversed(mems))

    cosine_a = mod.compute_cosine_matrix(mems)
    cosine_b = mod.compute_cosine_matrix(mems_reversed)

    candidates_a = mod.build_eligible_pairs(
        mems,
        cosine_a,
        edges_by_pair={},
        band=(-1.0, 1.0),
        require_no_edge=False,
        half_open=False,
    )
    candidates_b = mod.build_eligible_pairs(
        mems_reversed,
        cosine_b,
        edges_by_pair={},
        band=(-1.0, 1.0),
        require_no_edge=False,
        half_open=False,
    )

    # Both candidate lists should be canonically sorted → identical.
    assert [c.canonical_key for c in candidates_a] == [c.canonical_key for c in candidates_b]

    rng_a = np.random.default_rng(42)
    rng_b = np.random.default_rng(42)
    out_a = mod.sample_stratum(candidates_a, n=5, rng=rng_a)
    out_b = mod.sample_stratum(candidates_b, n=5, rng=rng_b)
    assert [c.canonical_key for c in out_a] == [c.canonical_key for c in out_b]


# ----------------------------------------------------------------------------
# Refinement #5: Stratum D ranking
# ----------------------------------------------------------------------------


def test_build_stratum_d_is_shared_tag_ranked_not_random(sampling_module):
    """Stratum D ranks by shared-tag overlap, top-k, NOT random sampling.

    Construct a fixture where one universe pair has obviously highest tag
    overlap with the picked pair, and verify it is selected first. A pair
    with zero overlap must NOT be selected.
    """
    mod = sampling_module
    rng = np.random.default_rng(0)

    def mk(idx: int, tags: tuple[str, ...]):
        v = rng.standard_normal(8)
        v /= np.linalg.norm(v)
        return mod.MemoryRecord(
            id=UUID(f"00000000-0000-0000-0000-{idx:012x}"),
            summary=f"m{idx}",
            tags=tags,
            vector=v,
            created_at_iso="2026-04-09T00:00:00+00:00",
        )

    def mk_candidate(src_idx: int, src_tags, dst_idx: int, dst_tags, cosine: float):
        return mod.Candidate(
            src=mk(src_idx, src_tags),
            dst=mk(dst_idx, dst_tags),
            cosine=cosine,
            has_existing_edge=False,
            existing_edge_type=None,
            synthetic_seed_edge=False,
        )

    picked = [mk_candidate(1, ("a", "b"), 2, ("c", "d"), cosine=0.5)]

    # Universe contains:
    #  - 4-tag overlap (max) → must be selected
    #  - 2-tag overlap → may be selected
    #  - 0-tag overlap → must NOT be selected
    universe = list(picked) + [
        mk_candidate(3, ("a", "b"), 4, ("c", "d"), cosine=0.45),  # 4 overlap
        mk_candidate(5, ("a", "x"), 6, ("y", "d"), cosine=0.50),  # 2 overlap
        mk_candidate(7, ("x", "y"), 8, ("z", "w"), cosine=0.50),  # 0 overlap
    ]

    out = mod.build_stratum_d(picked, universe, n=2)
    assert len(out) == 2

    out_ids = {c.src.id_str for c in out} | {c.dst.id_str for c in out}
    # Highest-overlap pair (idx 3-4) must be selected
    assert "00000000-0000-0000-0000-000000000003" in out_ids
    assert "00000000-0000-0000-0000-000000000004" in out_ids
    # Zero-overlap pair (idx 7-8) must NOT be selected
    assert "00000000-0000-0000-0000-000000000007" not in out_ids
    assert "00000000-0000-0000-0000-000000000008" not in out_ids


def test_build_stratum_d_is_deterministic(sampling_module):
    """Two calls with identical input give byte-identical output."""
    mod = sampling_module
    rng = np.random.default_rng(0)

    def mk(idx: int, tags: tuple[str, ...]):
        v = rng.standard_normal(8)
        v /= np.linalg.norm(v)
        return mod.MemoryRecord(
            id=UUID(f"00000000-0000-0000-0000-{idx:012x}"),
            summary=f"m{idx}",
            tags=tags,
            vector=v,
            created_at_iso="2026-04-09T00:00:00+00:00",
        )

    def mk_candidate(src_idx: int, src_tags, dst_idx: int, dst_tags, cosine: float):
        return mod.Candidate(
            src=mk(src_idx, src_tags),
            dst=mk(dst_idx, dst_tags),
            cosine=cosine,
            has_existing_edge=False,
            existing_edge_type=None,
            synthetic_seed_edge=False,
        )

    picked = [
        mk_candidate(1, ("a", "b"), 2, ("c", "d"), cosine=0.5),
        mk_candidate(11, ("e",), 12, ("f",), cosine=0.55),
    ]
    universe = list(picked) + [
        mk_candidate(3, ("a", "b"), 4, ("c",), cosine=0.45),
        mk_candidate(5, ("a", "x"), 6, ("y", "d"), cosine=0.50),
        mk_candidate(13, ("e", "f"), 14, ("g",), cosine=0.42),
        mk_candidate(15, ("p", "q"), 16, ("r",), cosine=0.55),
    ]

    out1 = mod.build_stratum_d(picked, universe, n=3)
    out2 = mod.build_stratum_d(picked, universe, n=3)
    assert [c.canonical_key for c in out1] == [c.canonical_key for c in out2]


# ----------------------------------------------------------------------------
# Edge filtering parity
# ----------------------------------------------------------------------------


def test_build_eligible_pairs_excludes_real_edges_when_required(sampling_module):
    """Stratum A semantics: ``require_no_edge=True`` excludes pairs with any
    non-synthetic edge but KEEPS pairs whose only edge is a synthetic seed."""
    mod = sampling_module
    mems = _fake_memories(mod, n=4, seed=99)
    cosine = mod.compute_cosine_matrix(mems)

    # Build canonical keys for the 6 unique pairs.
    keys = [
        (
            min(mems[i].id_str, mems[j].id_str),
            max(mems[i].id_str, mems[j].id_str),
        )
        for i in range(4)
        for j in range(i + 1, 4)
    ]

    edges_by_pair = {
        # Real edge → must be excluded under require_no_edge=True
        keys[0]: ("related_to", 0.7),
        # Synthetic seed → must be KEPT under require_no_edge=True
        keys[1]: ("semantic_similarity", 0.3),
        # High-weight semantic → real, must be excluded
        keys[2]: ("semantic_similarity", 0.8),
    }

    candidates = mod.build_eligible_pairs(
        mems,
        cosine,
        edges_by_pair,
        band=(-1.0, 1.0),
        require_no_edge=True,
        half_open=False,
    )
    candidate_keys = {c.canonical_key for c in candidates}

    assert keys[0] not in candidate_keys, "real edge should be excluded"
    assert keys[1] in candidate_keys, "synthetic seed should NOT block sampling"
    assert keys[2] not in candidate_keys, "high-weight semantic is real → excluded"
