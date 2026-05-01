"""Tests for the high-dim clusterer (Stage [D]).

Pins the AC-pinned design contract: KMeans MUST run on the
high-dimensional embedding matrix, NOT on a 2D UMAP projection.

The contract is enforced via two layers:

1. ``cluster_high_dim`` raises ``ValueError`` if it receives an
   ndarray with ``embedding_dim < 16``. 2 is what UMAP outputs; the
   guard catches the documented "wrong stage wired up first" bug.

2. ``test_kmeans_receives_high_dim_input`` patches
   ``sklearn.cluster.KMeans`` and asserts the array shape passed to
   ``fit_predict`` matches the input embedding_dim, not 2.

Plus quality-metric sanity checks that the run does not silently
return zeros for a clearly clusterable distribution.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from services.analysis.clusterer import cluster_high_dim


def _make_clusterable_embeddings(
    n_per_cluster: int = 30,
    n_clusters: int = 4,
    embedding_dim: int = 384,
    seed: int = 7,
) -> np.ndarray:
    """Construct a clearly clusterable high-dim point cloud.

    Each cluster is a Gaussian blob centered at a random point on
    the unit sphere with small isotropic noise. This is dense enough
    that silhouette > 0.3 is expected.
    """
    rng = np.random.default_rng(seed)
    centers = rng.normal(0.0, 1.0, size=(n_clusters, embedding_dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True) + 1e-12
    rows = []
    for c in centers:
        noise = rng.normal(0.0, 0.05, size=(n_per_cluster, embedding_dim))
        rows.append(c[None, :] + noise)
    return np.vstack(rows).astype(np.float32)


def test_rejects_2d_input() -> None:
    """A 2D-shaped embedding (UMAP output) MUST be rejected.

    This is the AC-pinned high-dim guard. If a future contributor
    accidentally swaps Stage [D] and Stage [E], this test fires.
    """
    rng = np.random.default_rng(7)
    fake_umap_output = rng.normal(size=(50, 2)).astype(np.float32)
    with pytest.raises(ValueError, match="embedding_dim"):
        cluster_high_dim(fake_umap_output)


def test_rejects_1d_input() -> None:
    """1D arrays are obviously wrong and must error early."""
    rng = np.random.default_rng(7)
    flat = rng.normal(size=(50,)).astype(np.float32)
    with pytest.raises(ValueError, match="2D"):
        cluster_high_dim(flat)


def test_rejects_too_few_rows() -> None:
    """KMeans requires at least 2 rows to make sense."""
    one_row = np.ones((1, 64), dtype=np.float32)
    with pytest.raises(ValueError, match="at least 2"):
        cluster_high_dim(one_row)


def test_kmeans_receives_high_dim_input() -> None:
    """AC-pinned: KMeans.fit gets the high-dim matrix, NOT 2D.

    Patches ``sklearn.cluster.KMeans`` and inspects what the
    clusterer actually passes to ``fit_predict``. The shape's
    second dimension MUST equal the input embedding_dim.
    """
    embeddings = _make_clusterable_embeddings(n_per_cluster=20, n_clusters=3, embedding_dim=384)
    captured: dict = {}

    class _RecordingKMeans:
        def __init__(self, **kwargs: object) -> None:
            captured["kwargs"] = kwargs

        def fit_predict(self, x: np.ndarray) -> np.ndarray:
            captured["fit_input_shape"] = x.shape
            # Return cluster labels matching n_clusters from kwargs.
            n = int(captured["kwargs"]["n_clusters"])  # type: ignore[arg-type]
            return np.arange(x.shape[0]) % n

        @property
        def cluster_centers_(self) -> np.ndarray:
            n = int(captured["kwargs"]["n_clusters"])  # type: ignore[arg-type]
            return np.ones((n, 384), dtype=np.float32)

    with patch("sklearn.cluster.KMeans", _RecordingKMeans):
        cluster_high_dim(embeddings)

    fit_shape = captured["fit_input_shape"]
    assert fit_shape == embeddings.shape, (
        f"KMeans.fit_predict received shape {fit_shape}, expected "
        f"{embeddings.shape}. The clusterer MUST pass the high-dim "
        "matrix, not a UMAP-2D projection."
    )
    # Specifically: the second dim is NOT 2.
    assert fit_shape[1] != 2, (
        "KMeans got embedding_dim=2; clusterer is feeding it the "
        "UMAP-2D output. This is the documented Phase 4 design flaw."
    )


def test_quality_metrics_are_meaningful_on_clusterable_data() -> None:
    """A clearly clusterable input produces non-trivial silhouette.

    Smoke test: 4 well-separated clusters, embedding_dim 64. The
    silhouette score should be well above 0 (typically ~0.3-0.7
    range on Gaussian blobs). If the clusterer ever silently returns
    zeros (e.g. accidentally passes 2D input that destroys the
    structure), this fires.
    """
    embeddings = _make_clusterable_embeddings(n_per_cluster=25, n_clusters=4, embedding_dim=64)
    result = cluster_high_dim(embeddings)

    assert result.n_clusters >= 2
    assert result.silhouette > 0.1, (
        f"silhouette={result.silhouette} on clusterable data; "
        "the clusterer may have lost the structure."
    )
    # Cluster sizes should be roughly balanced for this synthetic data.
    sizes = np.bincount(result.labels)
    assert sizes.min() > 0, "every cluster must have at least 1 member"
