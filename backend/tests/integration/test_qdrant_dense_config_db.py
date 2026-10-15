"""#1990: dense vector layout reconciliation against a live Qdrant.

The unit suite pins the argument shapes the reconciler sends; this proves the
round trip on a real server: a collection created as fp32 gets int8 + on_disk
from the settings, a second pass sends nothing, the search path accepts the
rescore block on both layouts, and setting the values back disables the
quantization again.

Skipped when ``QDRANT_URL`` is unreachable. Writes throwaway
``kagura_memories_it_*`` collections. Run with ``make test-integration``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, SparseVector

from config.settings import get_settings
from db import qdrant as qdrant_module
from db.qdrant import (
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    KAGURA_MEMORIES_VECTOR_NAME,
    DenseVectorState,
    apply_dense_vector_config,
    apply_dense_vector_config_to_all,
    ensure_kagura_memories_collection,
    search_memories_qdrant,
)

_DIM = 8


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://localhost:6333")


@pytest_asyncio.fixture(loop_scope="session")
async def qdrant(monkeypatch) -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(url=_qdrant_url(), timeout=10)
    try:
        await client.get_collections()
    except Exception as exc:  # noqa: BLE001 — intentional broad skip guard
        await client.close()
        pytest.skip(f"Qdrant unreachable at {_qdrant_url()}: {exc}")
    monkeypatch.setattr(qdrant_module, "_qdrant_client", client)
    yield client
    await client.close()


@pytest.fixture
def dense_settings(monkeypatch) -> Callable[[str, bool], None]:
    """Set the two dense-layout settings on the process Settings instance."""

    def _set(quantization: str = "none", on_disk: bool = False) -> None:
        monkeypatch.setattr(get_settings(), "qdrant_dense_quantization", quantization)
        monkeypatch.setattr(get_settings(), "qdrant_dense_on_disk", on_disk)

    _set()
    return _set


async def _state(client: AsyncQdrantClient, name: str) -> DenseVectorState | None:
    return qdrant_module._read_dense_state(await client.get_collection(name))


def _name() -> str:
    return f"kagura_memories_it_{uuid4().hex[:12]}"


class TestReconcileRoundTrip:
    async def test_fp32_collection_gets_int8_on_disk_then_back(self, qdrant, dense_settings):
        """none → int8+on_disk → (no-op) → none, each step visible on the server."""
        name = _name()
        try:
            await ensure_kagura_memories_collection(_DIM, name)
            assert await _state(qdrant, name) == DenseVectorState("none", False)

            dense_settings("int8", True)
            assert await apply_dense_vector_config(name) is True
            assert await _state(qdrant, name) == DenseVectorState("int8", True)

            # A second pass with the same settings sends nothing.
            assert await apply_dense_vector_config(name) is False
            # ensure() on the existing collection reconciles through the same
            # path and must not fail on an already-converted collection.
            await ensure_kagura_memories_collection(_DIM, name)
            assert await _state(qdrant, name) == DenseVectorState("int8", True)

            dense_settings("none", False)
            assert await apply_dense_vector_config(name) is True
            assert await _state(qdrant, name) == DenseVectorState("none", False)
            assert await apply_dense_vector_config(name) is False
        finally:
            await qdrant.delete_collection(name)

    async def test_startup_pass_covers_every_kagura_collection(self, qdrant, dense_settings):
        """apply_dense_vector_config_to_all converts the fp32 one, reports the matching one."""
        fp32 = _name()
        already = _name()
        try:
            await ensure_kagura_memories_collection(_DIM, fp32)
            dense_settings("int8", False)
            await ensure_kagura_memories_collection(_DIM, already)
            assert await _state(qdrant, already) == DenseVectorState("int8", False)

            results = await apply_dense_vector_config_to_all()

            assert results[fp32] is True
            assert results[already] is False
            assert await _state(qdrant, fp32) == DenseVectorState("int8", False)
        finally:
            await qdrant.delete_collection(fp32)
            await qdrant.delete_collection(already)


class TestSearchOnBothLayouts:
    @pytest.mark.parametrize("quantization,on_disk", [("none", False), ("int8", True)])
    async def test_dense_search_returns_the_nearest_point(
        self, qdrant, dense_settings, quantization, on_disk
    ):
        """The rescore block is accepted by the server on both layouts (#1990)."""
        dense_settings(quantization, on_disk)
        name = _name()
        workspace_id = str(uuid4())
        context_id = str(uuid4())
        user_id = f"user_{uuid4().hex[:8]}"
        try:
            await ensure_kagura_memories_collection(_DIM, name)
            assert await _state(qdrant, name) == DenseVectorState(quantization, on_disk)

            points = []
            for i in range(4):
                vector = [0.0] * _DIM
                vector[i] = 1.0
                points.append(
                    PointStruct(
                        id=str(uuid4()),
                        vector={
                            KAGURA_MEMORIES_VECTOR_NAME: vector,
                            KAGURA_MEMORIES_BM25_VECTOR_NAME: SparseVector(
                                indices=[i], values=[1.0]
                            ),
                        },
                        payload={
                            "workspace_id": workspace_id,
                            "context_id": context_id,
                            "user_id": user_id,
                            "axis": i,
                        },
                    )
                )
            await qdrant.upsert(collection_name=name, points=points, wait=True)

            query = [0.0] * _DIM
            query[2] = 0.9
            query[3] = 0.1
            results = await search_memories_qdrant(
                user_id=user_id,
                query_vector=query,
                workspace_id=workspace_id,
                context_id=context_id,
                limit=2,
                collection_name=name,
            )

            assert [r["payload"]["axis"] for r in results] == [2, 3]
        finally:
            await qdrant.delete_collection(name)
