#!/usr/bin/env python3
"""Print every kagura_memories* collection with its dense vector layout.

Shows what the #1990 reconciler sees: status (``green`` once the optimizer
has finished rebuilding segments after a quantization / on_disk change),
point counts, the dense vector's dimension and ``on_disk`` flag, and the
collection-level quantization. Run from ``backend/``:

    uv run python scripts/check_qdrant_collections.py
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from db.qdrant import (  # noqa: E402
    KAGURA_MEMORIES_COLLECTION,
    KAGURA_MEMORIES_VECTOR_NAME,
    get_qdrant_client,
)


def _quantization_label(config: Any) -> str:
    quantization = getattr(config, "quantization_config", None)
    if quantization is None:
        return "none"
    scalar = getattr(quantization, "scalar", None)
    if scalar is not None:
        scalar_type = getattr(scalar, "type", None)
        return str(getattr(scalar_type, "value", scalar_type))
    return type(quantization).__name__


def _dense_label(params: Any) -> str:
    vectors = getattr(params, "vectors", None)
    if isinstance(vectors, dict):
        dense = vectors.get(KAGURA_MEMORIES_VECTOR_NAME)
        if dense is None:
            return "dense: missing (named vectors: " + ", ".join(sorted(vectors)) + ")"
        return f"dense: dims={dense.size} on_disk={bool(dense.on_disk)}"
    if vectors is None:
        return "dense: missing"
    return f"dense: anonymous dims={vectors.size} on_disk={bool(vectors.on_disk)}"


async def check_collections() -> None:
    """List the collections and the layout of each kagura_memories* one."""
    client = get_qdrant_client()

    try:
        collections = await client.get_collections()
    except Exception as e:  # noqa: BLE001 — a CLI report, not a service path
        print(f"Failed to connect to Qdrant: {e}")
        return

    names = sorted(c.name for c in collections.collections)
    if not names:
        print("No collections found.")
        return

    print(f"{len(names)} collection(s): {', '.join(names)}")
    print()
    for name in names:
        if not name.startswith(KAGURA_MEMORIES_COLLECTION):
            continue
        try:
            info = await client.get_collection(name)
        except Exception as e:  # noqa: BLE001 — keep reporting the other collections
            print(f"{name}  error={e}")
            continue
        params = getattr(info.config, "params", None)
        print(
            f"{name}  status={info.status.value if hasattr(info.status, 'value') else info.status}"
            f"  points={info.points_count or 0:,}"
            f"  indexed={info.indexed_vectors_count or 0:,}"
            f"  {_dense_label(params)}"
            f"  quantization={_quantization_label(info.config)}"
        )
        if info.payload_schema:
            print(f"  payload indexes: {', '.join(sorted(info.payload_schema))}")


if __name__ == "__main__":
    asyncio.run(check_collections())
