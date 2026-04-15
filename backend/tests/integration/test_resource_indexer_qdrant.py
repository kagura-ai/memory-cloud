"""Integration test for Issue #324: Qdrant named-vector upsert contract.

Verifies the on-wire contract between the resource indexer and a real Qdrant
instance configured with named vectors (`dense` + sparse `bm25`). The unit
test in `tests/services/test_resource_indexer.py` asserts that the indexer
*produces* `PointStruct(vector={"dense": ...})`; this test asserts that a
real Qdrant accepts that shape and rejects the pre-fix anonymous shape.

Local-only: this test is not wired into CI yet. Run with:

    make test-integration

It skips automatically when `QDRANT_URL` is unreachable so it never becomes
a mystery failure for contributors without docker compose running.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    Modifier,
    PointStruct,
    SparseVectorParams,
    VectorParams,
)

from db.qdrant import KAGURA_MEMORIES_VECTOR_NAME

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
            "bm25": SparseVectorParams(modifier=Modifier.IDF),
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
