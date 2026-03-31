"""Backfill tags_text field for existing Qdrant memories.

Issue #67: Existing memories have tags but no tags_text field,
so BM25 fulltext search cannot match against tags.

This script reads all points from Qdrant, generates tags_text from
the existing tags array, and updates the payload.

Usage:
    cd backend && python -m scripts.backfill_tags_text

Environment:
    QDRANT_URL (default: http://localhost:6333)
"""

import asyncio
import sys

from qdrant_client import AsyncQdrantClient

QDRANT_URL = "http://localhost:6333"
COLLECTION = "kagura_memories"
BATCH_SIZE = 100


async def backfill():
    """Backfill tags_text for all points missing it."""
    client = AsyncQdrantClient(url=QDRANT_URL)

    updated = 0
    skipped = 0
    offset = None

    print(f"Backfilling tags_text in collection '{COLLECTION}'...")

    while True:
        result = await client.scroll(
            collection_name=COLLECTION,
            limit=BATCH_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, next_offset = result

        if not points:
            break

        for point in points:
            tags = point.payload.get("tags")
            existing_tags_text = point.payload.get("tags_text")

            # Skip if already has tags_text or no tags
            if existing_tags_text is not None or not tags:
                skipped += 1
                continue

            # Generate tags_text (pre-lowercased, consistent with memory_service.py)
            tags_text = " ".join(t.lower() for t in tags) if isinstance(tags, list) else ""

            if not tags_text:
                skipped += 1
                continue

            # Update payload
            await client.set_payload(
                collection_name=COLLECTION,
                payload={"tags_text": tags_text},
                points=[point.id],
            )
            updated += 1

        if not next_offset:
            break
        offset = next_offset

        print(f"  Progress: {updated} updated, {skipped} skipped...")

    await client.close()
    print(f"\nDone. Updated: {updated}, Skipped: {skipped}")
    return updated


if __name__ == "__main__":
    count = asyncio.run(backfill())
    sys.exit(0 if count >= 0 else 1)
