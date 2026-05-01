"""Stage [D]: KMeans clustering on **high-dimensional** embeddings.

This stage receives the raw embedding matrix from Stage [C]
(``vector_pull``) and produces cluster assignments + quality metrics.

CRITICAL: ``KMeans.fit`` MUST be called on the high-dimensional input.
The 2D UMAP projection produced by Stage [E] (``projector``) is for
**visualization only** — clustering on UMAP-2D output is the design
flaw of the previous-generation kouchou-ai pipeline (the "shape on
the screen does not match the topical structure" failure mode).
``test_clusterer_input_dimensionality`` asserts that ``KMeans.fit``
is called with the embedding-model dimensionality, not 2.

Algorithm (issue #495 spec):

- ``n_clusters = ceil(sqrt(n))`` — empirically reasonable density
  (~89 memory/cluster at n=8000). Outside the 1000-50000 sweet spot
  the heuristic degrades; v1.5 will parameterize this.
- ``random_state=42`` — deterministic across re-runs of the same
  context for reproducibility (the cluster id is then stable enough
  that the F4 frontend's color palette makes intuitive sense).
- ``n_init=10`` — 10 random initializations, sklearn picks the best
  by inertia. The default in sklearn >= 1.4 is ``n_init='auto'``
  (10 for k-means++); we pin explicitly for cross-version stability.

Quality metrics returned to the orchestrator (recorded in
``memory_analyses.quality`` JSONB):

- ``silhouette``       Mean silhouette on a 1000-sample subset.
                       Full silhouette is O(n^2) which kills the
                       <90s budget at n=8000.
- ``size_variance``    Variance of cluster sizes / mean cluster size.
                       Detects degenerate "one big cluster + many
                       singletons" runs.
- ``outlier_ratio``    Fraction of memories in clusters of size < 3.
                       UI threshold for the "outlier" cluster bucket.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)


# Silhouette is O(n^2) — capping the sample size keeps it bounded
# even when the full memory set is 50000. 1000 is plenty for a
# stable estimate of the mean silhouette.
_SILHOUETTE_SAMPLE_CAP = 1000

# Sizes <= this are considered "outlier" for UI grouping. Matches
# the prototype's "Outlier cluster" pattern (a few singleton-ish
# clusters get grouped into one swatch).
_OUTLIER_SIZE_THRESHOLD = 3


@dataclass(frozen=True)
class ClusterResult:
    """Output of Stage [D] for downstream stages.

    Attributes:
        labels: Integer cluster_index per input row, shape (n,).
        centroids: KMeans cluster centers in HIGH-DIM space,
            shape (n_clusters, embedding_dim). The high-dim
            centroid is what Stage [F] (representative selection)
            uses to find the 5 nearest memory.summary per cluster.
        n_clusters: ceil(sqrt(n)).
        silhouette: Mean silhouette score on a sampled subset.
        size_variance: Variance of cluster sizes / mean cluster
            size — high values indicate degenerate runs.
        outlier_ratio: Fraction of memories in clusters of size
            <= ``_OUTLIER_SIZE_THRESHOLD``.
    """

    labels: np.ndarray
    centroids: np.ndarray
    n_clusters: int
    silhouette: float
    size_variance: float
    outlier_ratio: float


def cluster_high_dim(embeddings: np.ndarray) -> ClusterResult:
    """KMeans cluster on high-dimensional embeddings.

    Args:
        embeddings: 2D array of shape (n, embedding_dim). MUST be
            the raw embedding output, NOT the UMAP-2D projection.
            ``embedding_dim`` is typically 1024 (Voyage), 1536
            (OpenAI text-embedding-3-small), or 3072 (large).

    Returns:
        ``ClusterResult`` with labels, centroids, and quality metrics.

    Raises:
        ValueError: ``embeddings`` is 1D, has < 2 rows, or has
            embedding_dim < 2 (would imply caller passed a
            UMAP-2D output by mistake — cheap sanity check).
    """
    # Local imports keep test mocking cheap and avoid sklearn
    # import-time cost on the hot path that doesn't need it.
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    if embeddings.ndim != 2:
        raise ValueError(f"embeddings must be 2D (n, embedding_dim); got ndim={embeddings.ndim}")
    n, embedding_dim = embeddings.shape
    if n < 2:
        raise ValueError(f"need at least 2 memories for clustering; got n={n}")
    if embedding_dim < 16:
        # 2D would mean UMAP output; 16 is a safe lower bound
        # for any real embedding model. Fail loud rather than
        # cluster-on-low-dim which is the documented design flaw.
        raise ValueError(
            f"embedding_dim={embedding_dim} is too low — clusterer must "
            "receive high-dim embeddings, not UMAP-2D output. See #495 "
            "Phase 4 architecture decision."
        )

    n_clusters = max(2, math.ceil(math.sqrt(n)))
    n_clusters = min(n_clusters, n - 1)  # KMeans requires k < n

    # sklearn 1.4+'s ``n_init`` accepts ``int | "auto"``. Some stubs only
    # type it as ``str``; pin to integer 10 explicitly via the kwargs
    # dict so Pyright sees ``Any`` for the value while we keep the
    # cross-version-deterministic 10-init behavior.
    km_kwargs: dict[str, object] = {
        "n_clusters": n_clusters,
        "random_state": 42,
        "n_init": 10,
    }
    km = KMeans(**km_kwargs)  # type: ignore[arg-type]
    labels = km.fit_predict(embeddings)
    centroids = km.cluster_centers_

    # Quality metrics
    if n > _SILHOUETTE_SAMPLE_CAP:
        # Stratified-ish sample by uniform random; silhouette is
        # robust to sampling even when clusters are imbalanced.
        rng = np.random.default_rng(42)
        sample_idx = rng.choice(n, size=_SILHOUETTE_SAMPLE_CAP, replace=False)
        sil = float(silhouette_score(embeddings[sample_idx], labels[sample_idx]))
    else:
        sil = float(silhouette_score(embeddings, labels))

    sizes = np.bincount(labels, minlength=n_clusters)
    size_variance = float(np.var(sizes) / max(np.mean(sizes), 1.0))
    outlier_clusters = int(np.sum(sizes <= _OUTLIER_SIZE_THRESHOLD))
    # Sum the memory count in undersized clusters, not the cluster count.
    outlier_memory_count = int(sizes[sizes <= _OUTLIER_SIZE_THRESHOLD].sum())
    outlier_ratio = float(outlier_memory_count / n)

    logger.info(
        "analysis_clusterer_complete",
        n=n,
        embedding_dim=embedding_dim,
        n_clusters=n_clusters,
        silhouette=sil,
        size_variance=size_variance,
        outlier_clusters=outlier_clusters,
        outlier_ratio=outlier_ratio,
    )

    return ClusterResult(
        labels=labels,
        centroids=centroids,
        n_clusters=n_clusters,
        silhouette=sil,
        size_variance=size_variance,
        outlier_ratio=outlier_ratio,
    )
