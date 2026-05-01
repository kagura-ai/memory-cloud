"""Stage [H]: per-cluster property statistics aggregation.

For each cluster, build the JSONB blob persisted to
``memory_analysis_clusters.property_stats``:

- ``tags``: top-K tag frequency (tag → count, sorted desc).
- ``types``: type distribution (memory.type → count).
- ``importance``: 4-bucket histogram with edges [0.0, 0.25, 0.5, 0.75, 1.0]
  — matches the precedent in ``services/sleep/...`` (memory pattern
  ``fa703658``: avg-only hides bimodality, 4-bucket exposes it).
- ``time``: 12-bucket histogram of memory.created_at across the
  ``[from, to]`` window. If no time window was filtered, the bucket
  edges span ``[min(created_at), max(created_at)]`` evenly.

The frontend's ``PropertyStats`` component renders these as a tag
bar chart, type pie, importance histogram, and time series — when
``focusedClusterId`` is set, the same component re-renders with
per-cluster aggregates.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from utils.datetime import to_utc_iso

# Top-K tags per cluster — past this, the bar chart becomes unreadable.
_TAG_TOP_K = 12

# 4-bucket histogram edges for importance. Closed-open intervals:
# [0.0, 0.25), [0.25, 0.5), [0.5, 0.75), [0.75, 1.0].
_IMPORTANCE_EDGES = (0.0, 0.25, 0.5, 0.75, 1.0001)

# 12-bucket histogram for time series — yearly when window is wide
# (>1 year), monthly for ~1 year, weekly for shorter windows. The
# count is fixed; the bin width adapts to the input range.
_TIME_BUCKETS = 12


@dataclass(frozen=True)
class MemoryFacets:
    """Sub-set of memory fields needed for property aggregation.

    Decoupled from ``models.memory.Memory`` so the labeler / reporter
    can pass plain dicts in tests without instantiating ORM rows.
    """

    type: str
    tags: list[str]
    importance: float
    created_at: datetime


def _importance_histogram(values: list[float]) -> list[int]:
    """4-bucket count over the [0.0, 1.0] range.

    Pattern matches memory ``fa703658``: bimodality detection is
    the point of separating into [0, 0.25), [0.25, 0.5),
    [0.5, 0.75), [0.75, 1.0].
    """
    if not values:
        return [0, 0, 0, 0]
    # np.histogram is edge-inclusive on the last bin, so the upper
    # edge 1.0001 swallows exact-1.0 importance values.
    counts, _ = np.histogram(values, bins=list(_IMPORTANCE_EDGES))
    return [int(c) for c in counts]


def _time_histogram(
    values: list[datetime],
    window_from: datetime | None,
    window_to: datetime | None,
) -> list[dict[str, Any]]:
    """12-bucket time series over the cluster's date range.

    Returns a list of ``{start: ISO, end: ISO, count: int}`` dicts —
    the frontend renders this directly as an area chart.
    """
    if not values:
        return []
    start = window_from or min(values)
    end = window_to or max(values)
    if start >= end:
        # Degenerate range (single instant) — single bucket.
        return [{"start": to_utc_iso(start), "end": to_utc_iso(end), "count": len(values)}]

    span = (end - start).total_seconds()
    bucket_seconds = span / _TIME_BUCKETS
    counts = [0] * _TIME_BUCKETS
    for v in values:
        offset = (v - start).total_seconds()
        idx = int(min(offset // bucket_seconds, _TIME_BUCKETS - 1))
        counts[max(idx, 0)] += 1

    # Reconstruct bucket boundaries from epoch timestamps in explicit
    # UTC so the JSON output carries an unambiguous Z suffix
    # regardless of the worker process's TZ env. ``Memory.created_at``
    # is naive UTC by repo convention (#489); ``to_utc_iso`` is
    # idempotent across naive/aware inputs.
    #
    # ``datetime.timestamp()`` on a NAIVE datetime interprets it in the
    # process's LOCAL timezone (Python stdlib behavior). The repo
    # convention is naive=UTC, so we attach ``tz=UTC`` before calling
    # timestamp() to force UTC interpretation. Without this, bucket
    # edges shift by the worker's UTC offset (e.g. JST workers would
    # produce buckets shifted by 9 hours).
    start_aware = start if start.tzinfo is not None else start.replace(tzinfo=UTC)
    start_epoch = start_aware.timestamp()
    out: list[dict[str, Any]] = []
    for i in range(_TIME_BUCKETS):
        bucket_start = datetime.fromtimestamp(start_epoch + i * bucket_seconds, tz=UTC)
        bucket_end = datetime.fromtimestamp(start_epoch + (i + 1) * bucket_seconds, tz=UTC)
        out.append(
            {
                "start": to_utc_iso(bucket_start),
                "end": to_utc_iso(bucket_end),
                "count": counts[i],
            }
        )
    return out


def aggregate_cluster_stats(
    memories: list[MemoryFacets],
    *,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
) -> dict[str, Any]:
    """Build the ``property_stats`` JSONB for one cluster.

    Args:
        memories: Memories assigned to this cluster.
        window_from: Lower bound for time histogram (typically
            ``params.from``). When None, uses
            ``min(created_at)``.
        window_to: Upper bound (``params.to`` / ``max(created_at)``).

    Returns:
        Dict with keys ``tags``, ``types``, ``importance``, ``time``.
    """
    if not memories:
        return {
            "tags": [],
            "types": {},
            "importance": [0, 0, 0, 0],
            "time": [],
        }

    tag_counter: Counter[str] = Counter()
    type_counter: Counter[str] = Counter()
    importances: list[float] = []
    timestamps: list[datetime] = []
    for m in memories:
        tag_counter.update(m.tags)
        type_counter[m.type] += 1
        importances.append(float(m.importance))
        timestamps.append(m.created_at)

    return {
        "tags": [{"tag": t, "count": c} for t, c in tag_counter.most_common(_TAG_TOP_K)],
        "types": dict(type_counter),
        "importance": _importance_histogram(importances),
        "time": _time_histogram(timestamps, window_from, window_to),
    }
