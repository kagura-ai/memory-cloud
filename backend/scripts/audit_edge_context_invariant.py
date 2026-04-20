#!/usr/bin/env python3
"""Audit neural_memory_edges for edge context invariant violations (#396 AC 6).

The invariant is:

    edge.workspace_id == src_memory.workspace_id == dst_memory.workspace_id
    edge.context_id   == src_memory.context_id   == dst_memory.context_id

PR #394 added a read-path Memory-scope filter as defense in depth, and the
sibling commit in this PR adds a write-path ``_validate_edge_context_invariant``
that blocks NEW violations at the repository level. This audit script handles
the PRE-EXISTING rows that may already violate the invariant — they need to
be surfaced (and, if desired, cleaned up) BEFORE the NOT NULL migration lands,
since the migration's CHECK / NOT NULL enforcement would otherwise fail validation
on these rows.

Violation classification:

  * ``missing_endpoint``    — src or dst memory row is gone (deleted or never
                              existed). Edge cannot be validated; safe to drop.
  * ``workspace_mismatch``  — endpoint memory's ``workspace_id`` differs from
                              edge's. Likely data from before migration 063 or
                              from a cross-workspace write bug. Drop.
  * ``context_mismatch``    — endpoint memory's ``context_id`` differs from
                              edge's. Same reasoning. Drop.

NULL-edge rows (edge.workspace_id OR edge.context_id IS NULL) are
INTENTIONALLY skipped here — they are handled by the Alembic migration (#396
AC 5), which has explicit backfill + orphan-delete logic for them.

Usage:
    python audit_edge_context_invariant.py              # report only
    python audit_edge_context_invariant.py --sample 20  # include 20 sample rows
    python audit_edge_context_invariant.py --fix        # delete violating edges
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import and_, delete, func, or_, select  # noqa: E402
from sqlalchemy.orm import aliased  # noqa: E402

from db.base import _get_session_factory  # noqa: E402
from models.memory import Memory, NeuralMemoryEdge  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


def _classify_row(row) -> str:
    """Return the single most specific violation classification for a row."""
    if row.src_id_found is None or row.dst_id_found is None:
        return "missing_endpoint"
    if (
        row.src_workspace_id != row.edge_workspace_id
        or row.dst_workspace_id != row.edge_workspace_id
    ):
        return "workspace_mismatch"
    return "context_mismatch"


async def audit_invariant(sample_limit: int = 5, fix: bool = False) -> dict[str, Any]:
    """Scan neural_memory_edges for invariant violations.

    Args:
        sample_limit: How many sample violating rows to log per category.
        fix: If True, delete violating edges. If False, report only.

    Returns:
        Counts dict with keys:
          * ``total_edges`` (int), ``scanned`` (int), ``violations`` (int),
            ``null_skipped`` (int), ``deleted`` (int) — scalar metrics.
          * ``by_category`` (dict[str, int]) — per-category violation counts.
          * ``samples`` (dict[str, list[Row]]) — per-category sample rows.
    """
    session_factory = _get_session_factory()
    async with session_factory() as session:
        src_mem = aliased(Memory, name="src_mem")
        dst_mem = aliased(Memory, name="dst_mem")

        # Violation predicate — shared between count/stream/delete so the three
        # paths cannot drift. ``IS DISTINCT FROM`` is PG's NULL-safe inequality:
        # plain ``!=`` evaluates to UNKNOWN when either side is NULL, so an
        # endpoint memory with NULL workspace_id/context_id (pre-063 data) would
        # slip past a ``src_mem.workspace_id != edge.workspace_id`` predicate
        # and never register as a violation. Copilot loop 2 catch.
        violation_predicate = and_(
            NeuralMemoryEdge.workspace_id.is_not(None),
            NeuralMemoryEdge.context_id.is_not(None),
            or_(
                src_mem.id.is_(None),
                dst_mem.id.is_(None),
                src_mem.workspace_id.is_distinct_from(NeuralMemoryEdge.workspace_id),
                src_mem.context_id.is_distinct_from(NeuralMemoryEdge.context_id),
                dst_mem.workspace_id.is_distinct_from(NeuralMemoryEdge.workspace_id),
                dst_mem.context_id.is_distinct_from(NeuralMemoryEdge.context_id),
            ),
        )

        # Totals via COUNT(*) — avoids materializing the full table in Python
        # just to call len(). On a multi-million-row neural_memory_edges this
        # is the difference between a multi-GB working set and a single scan.
        total_edges = (
            await session.execute(select(func.count()).select_from(NeuralMemoryEdge))
        ).scalar_one()

        null_skipped = (
            await session.execute(
                select(func.count())
                .select_from(NeuralMemoryEdge)
                .where(
                    or_(
                        NeuralMemoryEdge.workspace_id.is_(None),
                        NeuralMemoryEdge.context_id.is_(None),
                    )
                )
            )
        ).scalar_one()

        violations_count = (
            await session.execute(
                select(func.count())
                .select_from(NeuralMemoryEdge)
                .outerjoin(src_mem, src_mem.id == NeuralMemoryEdge.src_id)
                .outerjoin(dst_mem, dst_mem.id == NeuralMemoryEdge.dst_id)
                .where(violation_predicate)
            )
        ).scalar_one()

        # Stream a bounded sample per category rather than materializing every
        # violation. We still need to classify each streamed row to bucket it,
        # so sample_limit * 3 (three categories) rows is the worst case we
        # buffer in Python at any time.
        sample_stmt = (
            select(
                NeuralMemoryEdge.id.label("edge_id"),
                NeuralMemoryEdge.user_id.label("edge_user_id"),
                NeuralMemoryEdge.src_id.label("src_id"),
                NeuralMemoryEdge.dst_id.label("dst_id"),
                NeuralMemoryEdge.workspace_id.label("edge_workspace_id"),
                NeuralMemoryEdge.context_id.label("edge_context_id"),
                src_mem.id.label("src_id_found"),
                src_mem.workspace_id.label("src_workspace_id"),
                src_mem.context_id.label("src_context_id"),
                dst_mem.id.label("dst_id_found"),
                dst_mem.workspace_id.label("dst_workspace_id"),
                dst_mem.context_id.label("dst_context_id"),
            )
            .select_from(NeuralMemoryEdge)
            .outerjoin(src_mem, src_mem.id == NeuralMemoryEdge.src_id)
            .outerjoin(dst_mem, dst_mem.id == NeuralMemoryEdge.dst_id)
            .where(violation_predicate)
            .execution_options(yield_per=max(1, sample_limit) * 3)
        )

        counts_by_category: dict[str, int] = {
            "missing_endpoint": 0,
            "workspace_mismatch": 0,
            "context_mismatch": 0,
        }
        samples_by_category: dict[str, list] = {k: [] for k in counts_by_category}

        sample_result = await session.stream(sample_stmt)
        async for row in sample_result:
            category = _classify_row(row)
            counts_by_category[category] += 1
            if len(samples_by_category[category]) < sample_limit:
                samples_by_category[category].append(row)
            # Stop streaming once every category has its quota AND the running
            # per-category counts have saturated the COUNT(*) number. Since the
            # COUNT(*) already gave us the authoritative total, we only need
            # the stream for sample rows and category bucket populations.
            if all(len(samples_by_category[c]) >= sample_limit for c in counts_by_category):
                if sum(counts_by_category.values()) >= violations_count:
                    break

        results: dict[str, Any] = {
            "total_edges": total_edges,
            "scanned": total_edges - null_skipped,
            "violations": violations_count,
            "null_skipped": null_skipped,
            "by_category": counts_by_category,
            "samples": samples_by_category,
            "deleted": 0,
        }

        if fix and violations_count > 0:
            # Single-statement DELETE ... WHERE id IN (SELECT ... WHERE <pred>)
            # — no Python-side ID list, no IN-clause parameter pressure. The
            # predicate is the same one the SELECT used, so whatever COUNT(*)
            # saw is exactly what gets deleted (no TOCTOU narrowing).
            #
            # ``in_(<select>)`` is passed the Select directly, NOT
            # ``.scalar_subquery()`` — scalar_subquery is for expressions
            # expecting a single row and raises when the subquery returns
            # multiple rows (which is exactly what we want here). Plain
            # ``in_(<select>)`` treats the Select as a row-set, which is the
            # correct IN-subquery shape.
            del_stmt = delete(NeuralMemoryEdge).where(
                NeuralMemoryEdge.id.in_(
                    select(NeuralMemoryEdge.id)
                    .select_from(NeuralMemoryEdge)
                    .outerjoin(src_mem, src_mem.id == NeuralMemoryEdge.src_id)
                    .outerjoin(dst_mem, dst_mem.id == NeuralMemoryEdge.dst_id)
                    .where(violation_predicate)
                )
            )
            del_result = await session.execute(del_stmt)
            await session.commit()
            results["deleted"] = del_result.rowcount or 0
            logger.warning(
                "audit_edge_context_invariant_deleted",
                deleted=results["deleted"],
                categories=counts_by_category,
            )

        return results


def print_report(results: dict) -> None:
    """Print formatted scan report."""
    print("\n" + "=" * 66)
    print("neural_memory_edges — Edge Context Invariant Audit (#396 AC 6)")
    print("=" * 66)
    print(f"Total edges in table:             {results['total_edges']:>8,}")
    print(f"Edges scanned (non-NULL):         {results['scanned']:>8,}")
    print(
        f"Edges skipped (NULL ws/ctx):      {results['null_skipped']:>8,}  "
        "→ handled by Alembic migration (AC 5)"
    )
    print(f"\nInvariant violations detected:    {results['violations']:>8,}")
    for category, count in results["by_category"].items():
        print(f"  {category:<25s} {count:>8,}")

    # Sample rows per category
    for category, rows in results["samples"].items():
        if not rows:
            continue
        print(f"\n  Sample {category} (up to {len(rows)}):")
        for row in rows:
            print(
                f"    edge_id={row.edge_id} user={row.edge_user_id} "
                f"ws={row.edge_workspace_id} ctx={row.edge_context_id}"
            )
            print(
                f"      src={row.src_id} "
                f"→ mem_ws={row.src_workspace_id} mem_ctx={row.src_context_id}"
            )
            print(
                f"      dst={row.dst_id} "
                f"→ mem_ws={row.dst_workspace_id} mem_ctx={row.dst_context_id}"
            )

    if results["deleted"] > 0:
        print(f"\nDeleted violating rows:           {results['deleted']:>8,}")

    print("=" * 66)

    if results["violations"] > 0 and results["deleted"] == 0:
        print("\n⚠️  Violations detected. Review the sample rows, then re-run with")
        print("    --fix to delete them before applying the NOT NULL migration.")
        sys.exit(1)
    elif results["violations"] == 0:
        print("\n✓ No invariant violations. Non-NULL edges are consistent.")
        sys.exit(0)
    else:
        print("\n✓ Violations cleaned up.")
        sys.exit(0)


async def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Audit neural_memory_edges for edge context invariant violations (#396 AC 6)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=5,
        help="Max sample rows to log per violation category (default: 5)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Delete violating edges (default: report only)",
    )
    args = parser.parse_args()

    results = await audit_invariant(sample_limit=args.sample, fix=args.fix)
    print_report(results)


if __name__ == "__main__":
    asyncio.run(main())
