"""Integration tests for the Qdrant named-vector write contract.

Verifies the on-wire contract between this repo's Qdrant writers and a real
Qdrant instance configured with named vectors (`dense` + sparse `bm25`).
Three classes live here, each pinned to a specific issue:

- TestNamedVectorUpsertContract — Issue #324: raw named-vector upsert shape
  (rejects the pre-fix anonymous vector, idempotent re-queue).
- TestResourceIndexerSparseBM25 — Issue #335 / PR #342: resource_indexer
  emits `bm25` alongside `dense`; BM25-only search hits resource points.
- TestMemoryWriteSparseBM25 — Issue #345: `add_memory_to_qdrant` (the
  memory-side write path) emits `bm25` alongside `dense`; symmetric to the
  resource-side tests above.

The unit tests at tests/services/test_resource_indexer.py assert what
indexers *produce*; this module asserts that a real Qdrant accepts those
shapes and that search behavior matches the contract.

Local-only: these tests are not wired into CI yet. Run with:

    make test-integration

Skips automatically when `QDRANT_URL` is unreachable so contributors without
`docker compose up qdrant` get a clear skip message instead of a cryptic
connection error.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    Modifier,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from db.qdrant import (
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    KAGURA_MEMORIES_VECTOR_NAME,
    add_memory_to_qdrant,
)
from utils.sparse_vector import build_document_sparse_vector, build_resource_sparse_vector
from utils.tokenizer import tokenize_for_search

_EMBEDDING_DIM = 512


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://localhost:6333")


@pytest.fixture(scope="module")
def qdrant_client() -> Iterator[QdrantClient]:
    """Sync Qdrant client for fast integration assertions.

    Skips the whole module when Qdrant is unreachable so contributors without
    `docker compose up qdrant` get a clear skip message instead of an opaque
    connection error.
    """
    client = QdrantClient(url=_qdrant_url(), timeout=5)
    try:
        client.get_collections()
    except Exception as exc:  # noqa: BLE001 — intentional broad skip guard
        pytest.skip(f"Qdrant unreachable at {_qdrant_url()}: {exc}")
    yield client
    client.close()


@pytest.fixture
def named_vector_collection(qdrant_client: QdrantClient) -> Iterator[str]:
    """Create a throwaway kagura_memories-shaped collection and delete it after.

    Shape mirrors `ensure_kagura_memories_collection` in `backend/src/db/qdrant.py`:
    one named dense vector + sparse bm25. This is the exact schema the resource
    indexer is writing against in production.
    """
    name = f"test_resource_indexer_{uuid.uuid4().hex[:12]}"
    qdrant_client.create_collection(
        collection_name=name,
        vectors_config={
            KAGURA_MEMORIES_VECTOR_NAME: VectorParams(
                size=_EMBEDDING_DIM, distance=Distance.COSINE
            ),
        },
        sparse_vectors_config={
            KAGURA_MEMORIES_BM25_VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF),
        },
    )
    try:
        yield name
    finally:
        qdrant_client.delete_collection(name)


class TestNamedVectorUpsertContract:
    """Regression tests for Issue #324."""

    def test_named_vector_upsert_succeeds(
        self, qdrant_client: QdrantClient, named_vector_collection: str
    ) -> None:
        """Upsert with `vector={"dense": [...]}` must land a point."""
        point_id = str(uuid.uuid4())
        qdrant_client.upsert(
            collection_name=named_vector_collection,
            points=[
                PointStruct(
                    id=point_id,
                    vector={KAGURA_MEMORIES_VECTOR_NAME: [0.1] * _EMBEDDING_DIM},
                    payload={"doc_id": "d1", "version": 1},
                )
            ],
            wait=True,
        )
        count = qdrant_client.count(collection_name=named_vector_collection, exact=True)
        assert count.count == 1

    def test_anonymous_vector_upsert_fails(
        self, qdrant_client: QdrantClient, named_vector_collection: str
    ) -> None:
        """Regression guard: the pre-fix shape (bare list) must be rejected.

        If Qdrant ever relaxes this and starts accepting anonymous vectors on
        named-vector collections, we want to know — the shape contract would
        have softened and our named-vector assumption would need revisiting.
        """
        with pytest.raises((UnexpectedResponse, ValueError)):
            qdrant_client.upsert(
                collection_name=named_vector_collection,
                points=[
                    PointStruct(
                        id=str(uuid.uuid4()),
                        vector=[0.1] * _EMBEDDING_DIM,  # anonymous — pre-#324 bug shape
                        payload={},
                    )
                ],
                wait=True,
            )

    def test_named_vector_upsert_is_idempotent(
        self, qdrant_client: QdrantClient, named_vector_collection: str
    ) -> None:
        """Re-queueing the same event (same point_id) must not create duplicates.

        The indexer builds point_id = uuid5(NAMESPACE_DNS, "resource_id:doc_id:v{version}"),
        which is stable across runs. This is the invariant that makes the
        Issue #324 backfill runbook safe — ops can re-queue failed
        indexer_state rows without worrying about duplicate points.
        """
        point_id = str(uuid.uuid4())
        point = PointStruct(
            id=point_id,
            vector={KAGURA_MEMORIES_VECTOR_NAME: [0.2] * _EMBEDDING_DIM},
            payload={"doc_id": "d1", "version": 1},
        )
        qdrant_client.upsert(collection_name=named_vector_collection, points=[point], wait=True)
        qdrant_client.upsert(collection_name=named_vector_collection, points=[point], wait=True)
        count = qdrant_client.count(collection_name=named_vector_collection, exact=True)
        assert count.count == 1


class TestResourceIndexerSparseBM25:
    """Issue #335: resource_indexer must send `bm25` alongside `dense`.

    These are contract tests against a real Qdrant — they verify the on-wire
    shape and hybrid-search behavior, not the indexer's internal assembly
    (covered by tests/services/test_resource_indexer.py).
    """

    def _upsert_resource_point(
        self,
        qdrant_client: QdrantClient,
        collection: str,
        point_id: str,
        content: str,
        with_bm25: bool = True,
    ) -> None:
        """Mirror what resource_indexer._apply_upsert produces on the wire."""
        vector: dict[str, Any] = {KAGURA_MEMORIES_VECTOR_NAME: [0.1] * _EMBEDDING_DIM}
        if with_bm25:
            indices, values = build_resource_sparse_vector(content)
            if indices and values:
                vector[KAGURA_MEMORIES_BM25_VECTOR_NAME] = SparseVector(
                    indices=indices, values=values
                )
        qdrant_client.upsert(
            collection_name=collection,
            points=[PointStruct(id=point_id, vector=vector, payload={"content": content})],
            wait=True,
        )

    def test_upsert_carries_both_dense_and_bm25(
        self, qdrant_client: QdrantClient, named_vector_collection: str
    ) -> None:
        """AC1: a resource-shaped point persists both `dense` and `bm25` vectors."""
        point_id = str(uuid.uuid4())
        self._upsert_resource_point(
            qdrant_client, named_vector_collection, point_id, "PostgreSQL の migration 戦略"
        )

        retrieved = qdrant_client.retrieve(
            collection_name=named_vector_collection,
            ids=[point_id],
            with_vectors=True,
        )
        assert len(retrieved) == 1
        vectors = retrieved[0].vector
        assert isinstance(vectors, dict)
        assert KAGURA_MEMORIES_VECTOR_NAME in vectors
        assert KAGURA_MEMORIES_BM25_VECTOR_NAME in vectors, (
            "AC1: resource points must carry the bm25 sparse vector"
        )

    def test_bm25_only_query_hits_resource_point(
        self, qdrant_client: QdrantClient, named_vector_collection: str
    ) -> None:
        """AC2: a sparse-only query against the bm25 vector finds resource points.

        Pre-#335, resource points carried no bm25 vector → BM25 score was
        always zero → `using="bm25"` queries returned nothing from resource
        data. We assert presence in the hit list, not score value (DB PhD:
        Modifier.IDF scoring drifts with collection size, so absolute scores
        are flaky).
        """
        point_id = str(uuid.uuid4())
        content = "Sudachi tokenizer による日本語の BM25 検索"
        self._upsert_resource_point(qdrant_client, named_vector_collection, point_id, content)

        # Query with the same content as the doc → guaranteed token overlap.
        # Use the doc-side encoder for the query to keep the test
        # self-contained; production uses build_query_sparse_vector but the
        # token space is the same.
        q_indices, q_values = build_resource_sparse_vector(content)
        hits = qdrant_client.query_points(
            collection_name=named_vector_collection,
            query=SparseVector(indices=q_indices, values=q_values),
            using=KAGURA_MEMORIES_BM25_VECTOR_NAME,
            limit=10,
        ).points
        assert any(h.id == point_id for h in hits), (
            "AC2: bm25-only search must surface resource points"
        )

    def test_dense_only_backward_compat(
        self, qdrant_client: QdrantClient, named_vector_collection: str
    ) -> None:
        """AC3: legacy points (dense-only, no bm25) still work for dense search.

        Pre-#335 data — already on disk — must continue to be discoverable
        via dense search even though they will never match a bm25-only query.
        This guards the migration window where new and old shapes coexist.
        """
        point_id = str(uuid.uuid4())
        self._upsert_resource_point(
            qdrant_client,
            named_vector_collection,
            point_id,
            "legacy point without bm25",
            with_bm25=False,
        )

        dense_hits = qdrant_client.query_points(
            collection_name=named_vector_collection,
            query=[0.1] * _EMBEDDING_DIM,
            using=KAGURA_MEMORIES_VECTOR_NAME,
            limit=10,
        ).points
        assert any(h.id == point_id for h in dense_hits), (
            "AC3: legacy dense-only points must remain searchable via dense vector"
        )

        # And bm25-only search must NOT return the legacy point — points
        # without a sparse vector contribute zero to sparse retrieval, which
        # is the contract that makes the migration window safe.
        q_indices, q_values = build_resource_sparse_vector("legacy point without bm25")
        sparse_hits = qdrant_client.query_points(
            collection_name=named_vector_collection,
            query=SparseVector(indices=q_indices, values=q_values),
            using=KAGURA_MEMORIES_BM25_VECTOR_NAME,
            limit=10,
        ).points
        assert all(h.id != point_id for h in sparse_hits), (
            "Sparse-vectorless points must not appear in bm25-only search results"
        )


def _wait_for_point(
    qdrant_client: QdrantClient,
    collection: str,
    point_id: str,
    *,
    timeout_s: float = 2.0,
    interval_s: float = 0.05,
) -> None:
    """Poll until a point becomes visible after a wait=False upsert.

    `add_memory_to_qdrant` issues the async upsert without `wait=True` (see
    backend/src/db/qdrant.py:376); production doesn't block because the
    ingest flow tolerates eventual visibility, but tests need determinism.
    Polling here avoids patching the production signature just for tests.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if qdrant_client.retrieve(collection_name=collection, ids=[point_id], with_vectors=False):
            return
        time.sleep(interval_s)
    raise AssertionError(f"Point {point_id} not visible in {collection} after {timeout_s}s")


def _build_memory_sparse_vector(content: str) -> tuple[list[int], list[float]]:
    """Build the bm25 sparse vector for a memory with a given content.

    Routes through `content_tokens` (weight 1.0) only, leaving summary/
    context_summary empty. This keeps the test's single-field doc weighting
    analogous to the resource-side precedent (`build_resource_sparse_vector`,
    weight 1.0) and avoids a misleading 3.0× term weight that would result
    from passing the same tokens to both `summary_tokens` (weight 2.0) and
    `content_tokens` (weight 1.0). Used for both the upsert side and the
    query side so upsert-time and query-time indices are guaranteed to
    match in the BM25-only search test.
    """
    tokens = tokenize_for_search(content)
    indices, values = build_document_sparse_vector(
        summary_tokens="",
        context_summary_tokens="",
        content_tokens=tokens,
        summary_reading="",
    )
    if not indices:
        raise AssertionError(
            f"Test prerequisite: tokenize_for_search({content!r}) produced no tokens"
        )
    return indices, values


async def _add_memory_via_production_path(
    collection: str,
    *,
    content: str,
    with_bm25: bool = True,
) -> str:
    """Upsert a Memory through the production `add_memory_to_qdrant` path.

    Symmetric to TestResourceIndexerSparseBM25._upsert_resource_point, but
    goes through the real memory-side write path — this is what makes the
    mirrored tests actually cover the #345 contract (a raw client.upsert
    would bypass the `sparse_indices and sparse_values` gate at qdrant.py:371).

    `add_memory_to_qdrant` validates workspace_id and context_id via
    `_validate_uuid_format`, so both must be parseable as UUIDs.
    """
    memory_id = uuid.uuid4()
    indices: list[int] | None
    values: list[float] | None
    if with_bm25:
        indices, values = _build_memory_sparse_vector(content)
    else:
        indices, values = None, None
    await add_memory_to_qdrant(
        user_id=str(uuid.uuid4()),
        memory_id=memory_id,
        vector=[0.1] * _EMBEDDING_DIM,
        payload={"summary": content},
        workspace_id=str(uuid.uuid4()),
        context_id=str(uuid.uuid4()),
        sparse_indices=indices,
        sparse_values=values,
        collection_name=collection,
    )
    return str(memory_id)


class TestMemoryWriteSparseBM25:
    """Issue #345: `add_memory_to_qdrant` must send `bm25` alongside `dense`.

    Symmetric to TestResourceIndexerSparseBM25 (Issue #335). The memory
    write path has emitted `bm25` since #16 but previously only had
    unit-test coverage; these contract tests against a real Qdrant guard
    against a refactor silently dropping the sparse vector.
    """

    async def test_add_memory_carries_both_dense_and_bm25(
        self, qdrant_client: QdrantClient, named_vector_collection: str
    ) -> None:
        """AC1: a memory point persists both `dense` and `bm25` vectors."""
        point_id = await _add_memory_via_production_path(
            named_vector_collection,
            content="PostgreSQL の migration 戦略",
        )
        _wait_for_point(qdrant_client, named_vector_collection, point_id)

        retrieved = qdrant_client.retrieve(
            collection_name=named_vector_collection,
            ids=[point_id],
            with_vectors=True,
        )
        assert len(retrieved) == 1
        vectors = retrieved[0].vector
        assert isinstance(vectors, dict)
        assert KAGURA_MEMORIES_VECTOR_NAME in vectors
        assert KAGURA_MEMORIES_BM25_VECTOR_NAME in vectors, (
            "AC1: memory points must carry the bm25 sparse vector"
        )

    async def test_bm25_only_query_hits_memory_point(
        self, qdrant_client: QdrantClient, named_vector_collection: str
    ) -> None:
        """AC2: a sparse-only query against the bm25 vector finds memory points.

        We assert presence in the hit list, not score value (Modifier.IDF
        scoring drifts with collection size, so absolute scores are flaky).
        """
        content = "Sudachi tokenizer による日本語 memory の BM25 検索"
        point_id = await _add_memory_via_production_path(named_vector_collection, content=content)
        _wait_for_point(qdrant_client, named_vector_collection, point_id)

        # Same content and encoder on both sides → guaranteed token overlap.
        # Production uses build_query_sparse_vector on the query side, but
        # the token space (the hash index space) is identical so presence
        # assertions hold regardless of which encoder we use here.
        q_indices, q_values = _build_memory_sparse_vector(content)
        hits = qdrant_client.query_points(
            collection_name=named_vector_collection,
            query=SparseVector(indices=q_indices, values=q_values),
            using=KAGURA_MEMORIES_BM25_VECTOR_NAME,
            limit=10,
        ).points
        assert any(h.id == point_id for h in hits), (
            "AC2: bm25-only search must surface memory points"
        )

    async def test_dense_only_backward_compat(
        self, qdrant_client: QdrantClient, named_vector_collection: str
    ) -> None:
        """AC3: a memory upserted without sparse stays searchable via dense.

        "backward compat" here names the sparse-omitted path (callers that
        pass `sparse_indices=None`, e.g. reindex/restore), not the pre-#16
        anonymous-vector shape — that regression is guarded by
        TestNamedVectorUpsertContract::test_anonymous_vector_upsert_fails.
        """
        content = "legacy memory without bm25"
        point_id = await _add_memory_via_production_path(
            named_vector_collection, content=content, with_bm25=False
        )
        _wait_for_point(qdrant_client, named_vector_collection, point_id)

        dense_hits = qdrant_client.query_points(
            collection_name=named_vector_collection,
            query=[0.1] * _EMBEDDING_DIM,
            using=KAGURA_MEMORIES_VECTOR_NAME,
            limit=10,
        ).points
        assert any(h.id == point_id for h in dense_hits), (
            "AC3: sparse-omitted memories must remain searchable via dense vector"
        )

        # bm25-only search must NOT return the sparse-omitted point —
        # memories without a sparse vector contribute zero to sparse
        # retrieval, which is the contract that makes reindex/restore safe.
        q_indices, q_values = _build_memory_sparse_vector(content)
        sparse_hits = qdrant_client.query_points(
            collection_name=named_vector_collection,
            query=SparseVector(indices=q_indices, values=q_values),
            using=KAGURA_MEMORIES_BM25_VECTOR_NAME,
            limit=10,
        ).points
        assert all(h.id != point_id for h in sparse_hits), (
            "Sparse-vectorless memories must not appear in bm25-only search results"
        )
