#!/usr/bin/env python3
"""Check for orphaned Qdrant points after Single Collection Migration.

Issue #273 M-4: Monitoring script to detect points without proper workspace_id/context_id.

This script scans the kagura_memories collection for:
1. Points missing workspace_id payload field
2. Points missing context_id payload field
3. Points with empty string workspace_id/context_id
4. Points with invalid UUID format

Usage:
    python check_orphaned_qdrant_points.py
    python check_orphaned_qdrant_points.py --fix  # Auto-delete orphaned points
"""

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from db.qdrant import get_qdrant_client, KAGURA_MEMORIES_COLLECTION
from utils.logger import get_logger

logger = get_logger(__name__)


def is_valid_uuid(value: str) -> bool:
    """Check if string is valid UUID format.

    Args:
        value: String to validate

    Returns:
        True if valid UUID, False otherwise
    """
    if not value or not isinstance(value, str):
        return False

    try:
        UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


async def check_orphaned_points(fix: bool = False) -> dict[str, int]:
    """Scan Qdrant collection for orphaned points.

    Args:
        fix: If True, delete orphaned points. If False, only report.

    Returns:
        Dictionary with counts:
            - total_points: Total points in collection
            - missing_workspace_id: Points without workspace_id
            - missing_context_id: Points without context_id
            - invalid_workspace_id: Points with invalid workspace_id UUID
            - invalid_context_id: Points with invalid context_id UUID
            - deleted: Number of points deleted (if fix=True)
    """
    client = get_qdrant_client()

    # Get collection info
    try:
        collection_info = await client.get_collection(KAGURA_MEMORIES_COLLECTION)
        total_points = collection_info.points_count
        logger.info(f"Scanning {total_points:,} points in {KAGURA_MEMORIES_COLLECTION}")
    except Exception as e:
        logger.error(f"Failed to get collection info: {e}")
        return {}

    # Scroll through all points
    orphaned_ids = []
    missing_workspace_id = 0
    missing_context_id = 0
    invalid_workspace_id = 0
    invalid_context_id = 0

    offset = None
    batch_size = 100
    scanned = 0

    while True:
        try:
            # Scroll batch
            points, next_offset = await client.scroll(
                collection_name=KAGURA_MEMORIES_COLLECTION,
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )

            if not points:
                break

            # Check each point
            for point in points:
                scanned += 1
                payload = point.payload or {}
                is_orphaned = False

                # Check workspace_id
                workspace_id = payload.get("workspace_id")
                if not workspace_id:
                    missing_workspace_id += 1
                    is_orphaned = True
                elif not is_valid_uuid(workspace_id):
                    invalid_workspace_id += 1
                    is_orphaned = True

                # Check context_id
                context_id = payload.get("context_id")
                if not context_id:
                    missing_context_id += 1
                    is_orphaned = True
                elif not is_valid_uuid(context_id):
                    invalid_context_id += 1
                    is_orphaned = True

                if is_orphaned:
                    orphaned_ids.append(point.id)
                    logger.warning(
                        f"Orphaned point detected: {point.id} "
                        f"(workspace_id={workspace_id}, context_id={context_id})"
                    )

            # Progress update
            if scanned % 1000 == 0:
                logger.info(f"Scanned {scanned:,} / {total_points:,} points...")

            # Check if done
            if next_offset is None:
                break

            offset = next_offset

        except Exception as e:
            logger.error(f"Error during scroll: {e}")
            break

    results = {
        "total_points": total_points,
        "scanned": scanned,
        "orphaned": len(orphaned_ids),
        "missing_workspace_id": missing_workspace_id,
        "missing_context_id": missing_context_id,
        "invalid_workspace_id": invalid_workspace_id,
        "invalid_context_id": invalid_context_id,
        "deleted": 0,
    }

    # Delete orphaned points if requested
    if fix and orphaned_ids:
        logger.warning(f"Deleting {len(orphaned_ids)} orphaned points...")
        try:
            await client.delete(
                collection_name=KAGURA_MEMORIES_COLLECTION,
                points_selector=orphaned_ids,
            )
            results["deleted"] = len(orphaned_ids)
            logger.info(f"Successfully deleted {len(orphaned_ids)} orphaned points")
        except Exception as e:
            logger.error(f"Failed to delete orphaned points: {e}")

    return results


def print_report(results: dict[str, int]) -> None:
    """Print formatted report of scan results.

    Args:
        results: Results dictionary from check_orphaned_points()
    """
    print("\n" + "=" * 60)
    print("Orphaned Qdrant Points Report")
    print("=" * 60)
    print(f"Total points in collection: {results['total_points']:,}")
    print(f"Points scanned: {results['scanned']:,}")
    print(f"\nOrphaned points found: {results['orphaned']:,}")
    print(f"  - Missing workspace_id: {results['missing_workspace_id']:,}")
    print(f"  - Missing context_id: {results['missing_context_id']:,}")
    print(f"  - Invalid workspace_id UUID: {results['invalid_workspace_id']:,}")
    print(f"  - Invalid context_id UUID: {results['invalid_context_id']:,}")

    if results['deleted'] > 0:
        print(f"\nDeleted orphaned points: {results['deleted']:,}")

    print("=" * 60)

    # Exit code based on orphaned count
    if results['orphaned'] > 0 and results['deleted'] == 0:
        print("\n⚠️  WARNING: Orphaned points detected!")
        print("   Run with --fix to delete them.")
        sys.exit(1)
    elif results['orphaned'] == 0:
        print("\n✓ No orphaned points found. Collection is clean.")
        sys.exit(0)
    else:
        print("\n✓ Orphaned points cleaned up.")
        sys.exit(0)


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Check for orphaned Qdrant points (missing workspace_id/context_id)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Delete orphaned points (default: only report)",
    )
    args = parser.parse_args()

    # Run scan
    results = await check_orphaned_points(fix=args.fix)

    if not results:
        print("✗ Failed to scan collection")
        sys.exit(1)

    # Print report
    print_report(results)


if __name__ == "__main__":
    asyncio.run(main())
