#!/usr/bin/env python3
"""Check Qdrant collections after reset_db."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from db.qdrant import get_qdrant_client, KAGURA_MEMORIES_COLLECTION


async def check_collections():
    """Check all Qdrant collections."""
    client = get_qdrant_client()

    try:
        # List all collections
        collections = await client.get_collections()
        print("=" * 60)
        print("Qdrant Collections")
        print("=" * 60)

        if not collections.collections:
            print("⚠️  No collections found!")
            return

        for c in collections.collections:
            print(f"\n📦 {c.name}")

        # Check kagura_memories specifically
        print(f"\n{'=' * 60}")
        print(f"Collection: {KAGURA_MEMORIES_COLLECTION}")
        print("=" * 60)

        try:
            info = await client.get_collection(KAGURA_MEMORIES_COLLECTION)
            print(f"✅ Collection exists")
            print(f"   Status: {info.status}")
            print(f"   Points: {info.points_count:,}")
            print(f"   Vectors: {info.vectors_count}")
            print(f"   Indexed: {info.indexed_vectors_count}")

            print(f"\n📋 Payload Schema:")
            if info.payload_schema:
                for key, schema in info.payload_schema.items():
                    print(f"   - {key}: {schema}")
            else:
                print("   (No schema yet - collection empty)")

            print(f"\n📊 Vector Config:")
            print(f"   Dimension: {info.config.params.vectors.size}")
            print(f"   Distance: {info.config.params.vectors.distance}")

        except Exception as e:
            print(f"❌ Collection '{KAGURA_MEMORIES_COLLECTION}' not found!")
            print(f"   Error: {e}")
            print(f"\n⚠️  Run: ensure_kagura_memories_collection() to create it")

    except Exception as e:
        print(f"❌ Failed to connect to Qdrant: {e}")


if __name__ == "__main__":
    asyncio.run(check_collections())
