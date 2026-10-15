#!/usr/bin/env python3
"""Print every kagura_memories* collection with its dense vector layout.

Shows what the #1990 reconciler sees: status (``green`` once the optimizer
has finished rebuilding segments after a quantization / on_disk change),
point counts, the dense vector's dimension and ``on_disk`` flag, and the
quantization — classified by the reconciler itself (``_read_dense_state``),
so what this prints is what the startup pass decides on. Run from ``backend/``:

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
    _read_dense_state,
    get_qdrant_client,
)


def _dense_label(info: Any) -> str:
    """The layout exactly as the #1990 reconciler classifies it."""
    state = _read_dense_state(info)
    params = getattr(info.config, "params", None)
    vectors = getattr(params, "vectors", None)
    if state is None:
        if isinstance(vectors, dict):
            return "dense: missing (named vectors: " + ", ".join(sorted(vectors)) + ")"
        if vectors is None:
            return "dense: missing"
        return f"dense: anonymous dims={vectors.size} (not reconciled)"
    dims = vectors[KAGURA_MEMORIES_VECTOR_NAME].size
    return f"dense: dims={dims} on_disk={state.on_disk}  quantization={state.quantization}"


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
        print(
            f"{name}  status={info.status.value if hasattr(info.status, 'value') else info.status}"
            f"  points={info.points_count or 0:,}"
            f"  indexed={info.indexed_vectors_count or 0:,}"
            f"  {_dense_label(info)}"
        )
        if info.payload_schema:
            print(f"  payload indexes: {', '.join(sorted(info.payload_schema))}")


if __name__ == "__main__":
    asyncio.run(check_collections())
