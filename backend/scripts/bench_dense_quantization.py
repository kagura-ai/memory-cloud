#!/usr/bin/env python3
"""Measure int8 scalar quantization against fp32 on the same Qdrant (#1990).

Independent of the embedding model: random unit vectors are written to two
throwaway collections with the production layout (named ``dense`` vector,
cosine) — one fp32 in RAM, one int8 with the fp32 originals on disk — and the
same queries run against both. Reported per query set:

- ``overlap@k``: share of the fp32 top-k that the quantized search returns,
  with the rescore the API uses (oversampling 2.0) and without it;
- p50 / p95 latency of each variant, sequential queries.

The search-quality benchmark with real queries (docs/search-quality-benchmark.md)
is the check for a deployment; this script answers "how much does int8 move
the ranking on this server" without a model. Run from ``backend/``:

    uv run python scripts/bench_dense_quantization.py --points 20000 --dims 2560 --queries 200

Both collections are deleted at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from uuid import uuid4

import numpy as np
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    QuantizationSearchParams,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    SearchParams,
    VectorParams,
)

DENSE = "dense"
OVERSAMPLING = 2.0


def _unit_rows(rng: np.random.Generator, n: int, dims: int) -> np.ndarray:
    rows = rng.standard_normal((n, dims), dtype=np.float32)
    rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    return rows


def _corpus(
    rng: np.random.Generator, points: int, dims: int, clusters: int, spread: float
) -> np.ndarray:
    """Unit vectors in ``clusters`` groups (``clusters=0``: unstructured noise).

    Real embeddings sit in neighbourhoods; pure Gaussian noise in 2560
    dimensions has every pair at cosine ~0, which makes the top-k a tie that
    any quantization reshuffles — the worst case, not the typical one.
    """
    if clusters <= 0:
        return _unit_rows(rng, points, dims)
    centers = _unit_rows(rng, clusters, dims)
    rows = centers[rng.integers(0, clusters, size=points)] + spread * _unit_rows(rng, points, dims)
    rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    return rows


async def _create(client: AsyncQdrantClient, name: str, dims: int, *, int8: bool) -> None:
    await client.create_collection(
        collection_name=name,
        vectors_config={
            DENSE: VectorParams(size=dims, distance=Distance.COSINE, on_disk=True if int8 else None)
        },
        quantization_config=(
            ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8, quantile=0.99, always_ram=True
                )
            )
            if int8
            else None
        ),
    )


async def _upsert(client: AsyncQdrantClient, name: str, vectors: np.ndarray, batch: int) -> None:
    for start in range(0, len(vectors), batch):
        chunk = vectors[start : start + batch]
        await client.upsert(
            collection_name=name,
            points=[
                PointStruct(id=start + i, vector={DENSE: row.tolist()})
                for i, row in enumerate(chunk)
            ],
            wait=True,
        )


async def _wait_green(client: AsyncQdrantClient, name: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    status = "unknown"
    while time.monotonic() < deadline:
        info = await client.get_collection(name)
        status = getattr(info.status, "value", str(info.status))
        if status == "green":
            return status
        await asyncio.sleep(1.0)
    return status


async def _search(
    client: AsyncQdrantClient,
    name: str,
    queries: np.ndarray,
    k: int,
    params: SearchParams | None,
) -> tuple[list[list[int]], list[float]]:
    ids: list[list[int]] = []
    latencies: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        result = await client.query_points(
            collection_name=name,
            query=q.tolist(),
            using=DENSE,
            limit=k,
            search_params=params,
        )
        latencies.append((time.perf_counter() - t0) * 1000.0)
        ids.append([int(p.id) for p in result.points])
    return ids, latencies


def _overlap(truth: list[list[int]], got: list[list[int]], k: int) -> float:
    return statistics.fmean(len(set(a) & set(b)) / k for a, b in zip(truth, got, strict=True))


def _pct(values: list[float], p: float) -> float:
    return float(np.percentile(values, p))


async def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--url", default=os.getenv("QDRANT_URL", "http://localhost:6333"))
    parser.add_argument("--points", type=int, default=20000)
    parser.add_argument("--dims", type=int, default=2560)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--batch", type=int, default=256)
    parser.add_argument("--seed", type=int, default=1990)
    parser.add_argument(
        "--clusters", type=int, default=200, help="corpus structure; 0 = unstructured noise"
    )
    parser.add_argument("--spread", type=float, default=0.5, help="noise around a cluster centre")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for green")
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    vectors = _corpus(rng, args.points, args.dims, args.clusters, args.spread)
    # Queries near existing points so the top-k is a real neighbourhood, not noise.
    base = vectors[rng.integers(0, args.points, size=args.queries)]
    queries = base + 0.3 * _unit_rows(rng, args.queries, args.dims)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)

    suffix = uuid4().hex[:8]
    fp32_name = f"bench_fp32_{suffix}"
    int8_name = f"bench_int8_{suffix}"
    client = AsyncQdrantClient(
        url=args.url, api_key=os.getenv("QDRANT_API_KEY") or None, timeout=120
    )
    try:
        await _create(client, fp32_name, args.dims, int8=False)
        await _create(client, int8_name, args.dims, int8=True)
        t0 = time.perf_counter()
        await _upsert(client, fp32_name, vectors, args.batch)
        await _upsert(client, int8_name, vectors, args.batch)
        print(
            f"upserted {args.points} x {args.dims} into both collections in {time.perf_counter() - t0:.1f}s"
        )
        for name in (fp32_name, int8_name):
            status = await _wait_green(client, name, args.timeout)
            print(f"{name}: status={status}")

        rescore = SearchParams(
            quantization=QuantizationSearchParams(
                ignore=False, rescore=True, oversampling=OVERSAMPLING
            )
        )
        no_rescore = SearchParams(
            quantization=QuantizationSearchParams(ignore=False, rescore=False, oversampling=1.0)
        )

        truth, lat_fp32 = await _search(client, fp32_name, queries, args.k, None)
        got_rescore, lat_rescore = await _search(client, int8_name, queries, args.k, rescore)
        got_plain, lat_plain = await _search(client, int8_name, queries, args.k, no_rescore)

        print()
        print(
            f"points={args.points} dims={args.dims} clusters={args.clusters} "
            f"spread={args.spread} queries={args.queries} k={args.k}"
        )
        print(f"{'variant':<34}{'overlap@1':>10}{'overlap@k':>10}{'p50 ms':>10}{'p95 ms':>10}")
        rows = [
            ("fp32 in RAM (baseline)", truth, lat_fp32),
            (f"int8 + on_disk, rescore x{OVERSAMPLING}", got_rescore, lat_rescore),
            ("int8 + on_disk, no rescore", got_plain, lat_plain),
        ]
        top1 = [ids[:1] for ids in truth]
        for label, got, lat in rows:
            print(
                f"{label:<34}"
                f"{_overlap(top1, [ids[:1] for ids in got], 1):>10.3f}"
                f"{_overlap(truth, got, args.k):>10.3f}"
                f"{_pct(lat, 50):>10.1f}{_pct(lat, 95):>10.1f}"
            )
    finally:
        for name in (fp32_name, int8_name):
            try:
                await client.delete_collection(name)
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                print(f"cleanup of {name} failed: {exc}")
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
