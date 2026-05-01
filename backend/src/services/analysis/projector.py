"""Stage [E]: UMAP 2D projection for visualization only.

The 2D coordinates produced here populate
``memory_analysis_assignments.x`` / ``.y``. They are consumed by
the F4 frontend (``ScatterPlot.tsx``) for the cluster overview map
— **never used for clustering decisions**. KMeans (Stage [D]) runs
on the high-dim embeddings; this projection is purely for human
visual inspection.

UMAP parameters (issue #495 Risks section):

- ``n_components=2`` — fixed for the scatter plot
- ``random_state=42`` — reproducibility across re-runs
- ``n_neighbors=15`` — sklearn-typical default; well-tuned for
  the 5000-20000 memory range
- ``min_dist=0.10`` — tighter than default 0.10 to keep cluster
  visual cohesion (matches the prototype's appearance)

Lazy-import: ``umap-learn`` pulls ``numba`` which JIT-compiles on
first use (~2-5s cold start per process). We import inside the
function so workers that never touch the analysis path don't pay
the JIT tax.
"""

from __future__ import annotations

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)


def project_to_2d(embeddings: np.ndarray) -> np.ndarray:
    """Reduce high-dim embeddings to 2D UMAP coordinates.

    Args:
        embeddings: 2D array of shape (n, embedding_dim). MUST be
            the same input passed to the clusterer (Stage [D]).

    Returns:
        2D coordinates, shape (n, 2). Float32 for memory frugality.

    Raises:
        ValueError: If ``embeddings`` is not 2D or has < 2 rows.
    """
    if embeddings.ndim != 2:
        raise ValueError(f"embeddings must be 2D; got ndim={embeddings.ndim}")
    n = embeddings.shape[0]
    if n < 2:
        raise ValueError(f"need at least 2 rows for UMAP; got n={n}")

    # Lazy import — see module docstring.
    import umap

    n_neighbors = min(15, n - 1)
    reducer = umap.UMAP(
        n_components=2,
        random_state=42,
        n_neighbors=n_neighbors,
        min_dist=0.10,
    )
    # ``UMAP.fit_transform`` is typed as a Union including ``coo_matrix``
    # in newer stubs because UMAP can return a sparse representation in
    # exotic configurations. With our default kwargs (n_components=2,
    # standard distance metric) it always returns a dense ndarray, so
    # round-trip through ``np.asarray`` to narrow the type for callers.
    raw = reducer.fit_transform(embeddings)
    coords_2d = np.asarray(raw, dtype=np.float32)
    logger.info(
        "analysis_projector_complete",
        n=n,
        embedding_dim=int(embeddings.shape[1]),
        x_range=[float(coords_2d[:, 0].min()), float(coords_2d[:, 0].max())],
        y_range=[float(coords_2d[:, 1].min()), float(coords_2d[:, 1].max())],
    )
    return coords_2d
