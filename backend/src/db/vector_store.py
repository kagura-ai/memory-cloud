"""Vector store backend abstraction ("Kagura Lite").

Kagura's vector operations historically live as module-level functions in
``db/qdrant.py``. This module introduces a thin :class:`VectorStore` Protocol
plus a backend selector so an alternative in-process engine (LanceDB, for the
self-hosted "Kagura Lite" edition) can be swapped in behind a feature flag
without rewriting the ~23 call sites that import ``db.qdrant``.

Design — minimal, behavior-preserving seam:

* Default backend is ``qdrant``. :func:`get_active_store` returns ``None`` in
  that case, so every ``db/qdrant.py`` public function runs its existing Qdrant
  code path unchanged (a single ``is None`` branch — zero behavior change and
  negligible overhead for the default deployment).
* When ``settings.vector_backend == "lance"``, the same Qdrant functions
  delegate to the :class:`LanceVectorStore` returned here. The fusion of
  semantic + BM25 results stays in ``services/search_service.py`` — the store
  only needs to return per-mode results in the same shape as the Qdrant
  functions, so no higher layer changes.

The Protocol method signatures intentionally mirror the corresponding
``db/qdrant.py`` functions so dispatch is a clean pass-through.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from utils.logger import get_logger

logger = get_logger(__name__)

# Mirrors db.qdrant.KAGURA_MEMORIES_COLLECTION. Duplicated as a literal here to
# keep this module import-light (importing db.qdrant would pull qdrant_client).
DEFAULT_COLLECTION = "kagura_memories"


@runtime_checkable
class VectorStore(Protocol):
    """Backend-agnostic vector store contract.

    Methods mirror the ``db/qdrant.py`` functions of the same role. Result
    shapes MUST match the Qdrant implementations exactly:

    * :meth:`search_semantic` → ``[{"id", "score", "payload", "embedding"}]``
    * :meth:`search_fulltext` → ``[{"id", "score", "payload"}]``
    """

    async def ensure_collection(
        self, embedding_dim: int = 512, collection_name: str = DEFAULT_COLLECTION
    ) -> None: ...

    async def add_memory(
        self,
        user_id: str,
        memory_id: UUID,
        vector: list[float],
        payload: dict[str, Any],
        workspace_id: str,
        context_id: str,
        sparse_indices: list[int] | None = None,
        sparse_values: list[float] | None = None,
        collection_name: str = DEFAULT_COLLECTION,
    ) -> None: ...

    async def search_semantic(
        self,
        user_id: str,
        query_vector: list[float],
        workspace_id: str,
        context_id: str | list[str],
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        is_shared_context: bool = False,
        collection_name: str = DEFAULT_COLLECTION,
        include_vectors: bool = False,
    ) -> list[dict]: ...

    async def search_fulltext(
        self,
        user_id: str,
        query: str,
        workspace_id: str,
        context_id: str | list[str],
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        is_shared_context: bool = False,
        collection_name: str = DEFAULT_COLLECTION,
    ) -> list[dict]: ...

    async def update_payload(
        self,
        memory_id: UUID,
        payload_updates: dict[str, Any],
        collection_name: str = DEFAULT_COLLECTION,
    ) -> None: ...

    async def delete_memory(
        self,
        user_id: str,
        memory_id: UUID,
        collection_name: str = DEFAULT_COLLECTION,
    ) -> None: ...

    async def delete_context_points(
        self,
        workspace_id: str,
        context_id: str,
        collection_name: str = DEFAULT_COLLECTION,
    ) -> int: ...


# Cached resolution. ``None`` is a valid resolved value (default Qdrant path),
# so a separate ``_resolved`` flag distinguishes "not yet resolved" from
# "resolved to None".
_store_singleton: VectorStore | None = None
_resolved: bool = False


def get_active_store() -> VectorStore | None:
    """Return the configured alternative VectorStore, or ``None`` for Qdrant.

    ``None`` signals ``db/qdrant.py`` to run its native Qdrant code path. This
    keeps the default (server) deployment completely unchanged — the only cost
    is one cached settings read and an ``is None`` check per call.

    Returns:
        A :class:`VectorStore` when ``vector_backend`` is a non-Qdrant backend
        (currently only ``"lance"``), else ``None``.

    Raises:
        ValueError: If ``vector_backend`` is set to an unknown value.
    """
    global _store_singleton, _resolved

    if not _resolved:
        from config.settings import get_settings

        settings = get_settings()
        backend = (settings.vector_backend or "qdrant").strip().lower()

        if backend in ("", "qdrant"):
            _store_singleton = None
        elif backend == "lance":
            from db.lance_store import LanceVectorStore

            _store_singleton = LanceVectorStore(db_path=settings.lance_db_path)
            logger.info("vector_backend_active", backend="lance", path=settings.lance_db_path)
        else:
            raise ValueError(
                f"Unknown vector_backend: {backend!r}. Supported: 'qdrant' (default), 'lance'."
            )
        _resolved = True

    return _store_singleton


def reset_vector_store() -> None:
    """Reset the cached backend resolution. Test-only helper."""
    global _store_singleton, _resolved
    _store_singleton = None
    _resolved = False
