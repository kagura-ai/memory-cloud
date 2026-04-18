"""PSI (Population Stability Index) computation for BM25 IDF distributions.

Issue #343 — Stats PhD spec:

    IDF_global(t) = ln( (N_global - df_global(t) + 0.5) / (df_global(t) + 0.5) + 1 )
    IDF_memory(t) = ln( (M       - df_memory(t) + 0.5) / (df_memory(t) + 0.5) + 1 )

Workflow:
    1. Filter terms by Cochran's rule (df_memory >= 5 AND df_global >= 5).
    2. Compute IDF_memory(t) and IDF_global(t) per surviving term.
    3. Bin terms into 10 quantiles by IDF_memory value.
    4. Compute P_memory(b) and P_global(b) (term-share per bin under each
       distribution).
    5. PSI = sum_b (P_global(b) - P_memory(b)) * ln(
                     (P_global(b)+eps) / (P_memory(b)+eps)
                  )
       with eps = 1 / |T| to keep zero-bins from making PSI undefined.
    6. Status: <0.10 stable, <0.25 minor_drift, >=0.25 significant_drift.

Min-N gates (cold-start false-positive guard):
    M >= MIN_MEMORY_POINTS  AND  |T| >= MIN_SURVIVING_TERMS  → compute PSI
    otherwise                                                → insufficient_data
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# Min-N gates from the Stats PhD review. Surfaced as constants so tests can
# reference them without re-deriving the rationale.
MIN_MEMORY_POINTS = 100  # binomial CI ~+/-10% at this sample size
MIN_SURVIVING_TERMS = 50  # chi^2 approximation needs ~5 terms per bin x 10 bins
MIN_DF = 5  # Cochran's rule: expected count >= 5

# Industry-standard PSI thresholds (recalibrate after #new-B golden eval lands).
PSI_MINOR_THRESHOLD = 0.10
PSI_SIGNIFICANT_THRESHOLD = 0.25

NUM_BINS = 10
TOP_DIVERGENT_TERMS_LIMIT = 20

PsiStatus = Literal["insufficient_data", "stable", "minor_drift", "significant_drift"]


@dataclass(frozen=True)
class PsiResult:
    """Output of compute_psi.

    Attributes:
        psi: PSI scalar, or None when status is insufficient_data.
        status: One of insufficient_data / stable / minor_drift /
            significant_drift. Pinned to the status enum stored in
            bm25_idf_drift_log.psi_status.
        num_terms: |T| after Cochran's filter (0 when insufficient_data
            because no points were sampled or no terms passed the filter).
        top_divergent_terms: Up to TOP_DIVERGENT_TERMS_LIMIT entries sorted
            by |IDF_memory(t) - IDF_global(t)| descending. Empty list when
            insufficient_data.
    """

    psi: float | None
    status: PsiStatus
    num_terms: int
    top_divergent_terms: list[dict]


def _bm25_idf(n_total: int, df: int) -> float:
    """Robertson BM25 IDF: ln((N - df + 0.5)/(df + 0.5) + 1).

    Mirrors Qdrant's Modifier.IDF formula. The +1 inside the log keeps the
    value non-negative even when df > N/2 (which Robertson's original form
    can produce — Qdrant's variant guards against negative IDF that would
    invert ranking).
    """
    return math.log((n_total - df + 0.5) / (df + 0.5) + 1.0)


def classify_status(psi: float) -> PsiStatus:
    """Map a numeric PSI to its industry-standard status bucket."""
    if psi < PSI_MINOR_THRESHOLD:
        return "stable"
    if psi < PSI_SIGNIFICANT_THRESHOLD:
        return "minor_drift"
    return "significant_drift"


def _quantile_bin_edges(values: list[float], n_bins: int) -> list[float]:
    """Compute n_bins+1 quantile edges over `values`.

    Uses linear interpolation between sorted values. Returns sorted edges
    starting with min(values) and ending with max(values). Caller is
    responsible for ensuring len(values) >= n_bins.
    """
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    edges: list[float] = []
    for i in range(n_bins + 1):
        # quantile q = i / n_bins; index = q * (n - 1) with linear interp.
        if i == 0:
            edges.append(sorted_vals[0])
            continue
        if i == n_bins:
            edges.append(sorted_vals[-1])
            continue
        idx = i * (n - 1) / n_bins
        lo = math.floor(idx)
        hi = math.ceil(idx)
        if lo == hi:
            edges.append(sorted_vals[lo])
        else:
            frac = idx - lo
            edges.append(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)
    return edges


def _bin_index(value: float, edges: list[float]) -> int:
    """Find the bin index in [0, len(edges)-2] for `value`.

    Edges are inclusive on the left, inclusive on the right for the last
    bin. A value equal to the rightmost edge falls into the last bin.
    """
    n_bins = len(edges) - 1
    # Binary search would be more efficient at scale, but with NUM_BINS=10
    # a linear scan is fine and keeps the code obvious.
    for i in range(n_bins):
        if value <= edges[i + 1]:
            return i
    return n_bins - 1


def compute_psi(
    *,
    df_memory: dict[int, int],
    df_global: dict[int, int],
    m_memory: int,
    n_global: int,
) -> PsiResult:
    """Compute PSI between memory-only and collection-global IDF distributions.

    Args:
        df_memory: Document frequency of each token index, restricted to
            memory-source documents. Keys are mmh3.hash(token) integers.
        df_global: Document frequency in the full collection (memory +
            resource). Same key space as df_memory.
        m_memory: Total number of memory-source documents.
        n_global: Total number of documents in the collection.

    Returns:
        PsiResult with `status`, `psi` scalar (or None), surviving term
        count, and top-divergent term snapshot.
    """
    # Min-N gate 1: not enough memory points to estimate IDF reliably.
    if m_memory < MIN_MEMORY_POINTS:
        return PsiResult(
            psi=None,
            status="insufficient_data",
            num_terms=0,
            top_divergent_terms=[],
        )

    # Apply Cochran's rule: a term must clear MIN_DF in BOTH distributions.
    # A term that exists in resource only (df_memory=0) is not a memory-side
    # ranking concern, so excluding it is correct.
    surviving = [
        idx for idx, df_m in df_memory.items() if df_m >= MIN_DF and df_global.get(idx, 0) >= MIN_DF
    ]

    # Min-N gate 2: too few surviving terms for the chi^2 binning approximation.
    if len(surviving) < MIN_SURVIVING_TERMS:
        return PsiResult(
            psi=None,
            status="insufficient_data",
            num_terms=len(surviving),
            top_divergent_terms=[],
        )

    # Compute per-term IDF under both distributions.
    idf_pairs: list[tuple[int, float, float, int, int]] = []
    for idx in surviving:
        df_m = df_memory[idx]
        df_g = df_global[idx]
        idf_m = _bm25_idf(m_memory, df_m)
        idf_g = _bm25_idf(n_global, df_g)
        idf_pairs.append((idx, idf_m, idf_g, df_m, df_g))

    # Bin by IDF_memory quantiles.
    memory_idfs = [p[1] for p in idf_pairs]
    edges = _quantile_bin_edges(memory_idfs, NUM_BINS)

    p_memory = [0] * NUM_BINS
    p_global_counts = [0] * NUM_BINS
    for pair in idf_pairs:
        p_memory[_bin_index(pair[1], edges)] += 1
        p_global_counts[_bin_index(pair[2], edges)] += 1

    n = len(surviving)
    eps = 1.0 / n  # Stats PhD: epsilon-smoothing, prevents log(0).
    psi = 0.0
    for b in range(NUM_BINS):
        p = p_memory[b] / n
        q = p_global_counts[b] / n
        psi += (q - p) * math.log((q + eps) / (p + eps))

    # Top-N divergent terms: sorted by |IDF_memory - IDF_global| desc.
    idf_pairs.sort(key=lambda row: abs(row[1] - row[2]), reverse=True)
    top = [
        {
            "index": idx,
            "df_memory": df_m,
            "df_global": df_g,
            "idf_memory": round(idf_m, 6),
            "idf_global": round(idf_g, 6),
            "delta": round(idf_m - idf_g, 6),
        }
        for idx, idf_m, idf_g, df_m, df_g in idf_pairs[:TOP_DIVERGENT_TERMS_LIMIT]
    ]

    return PsiResult(
        psi=psi,
        status=classify_status(psi),
        num_terms=n,
        top_divergent_terms=top,
    )
