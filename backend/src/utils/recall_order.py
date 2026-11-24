"""recall's ``order="date"``: the same results, oldest first (#3004).

The search, the ranking and everything derived from it (``confidence``, which
rows carry content, which rows a response budget cuts) stay as relevance
decides. Only the presentation moves: each row gets its day and the rows are
sorted by it, ties keeping relevance order (a stable sort).

A row's day is the event's own day (``event_start``), else the day the fact
was stated (``observed_at``), else the day of ``created_at``, each read the
way the row already renders it (UTC, no timezone conversion of its own).
"""

from __future__ import annotations

from typing import Any, Literal

from utils.datetime import to_utc_iso
from utils.event_dates import event_weekday
from utils.observed_at import observed_weekday

# recall's ``order`` values; "relevance" is the default and the response as before.
RECALL_ORDERS = ("relevance", "date")

DateSource = Literal["event", "observed", "created"]


def row_date(row: Any) -> tuple[str, DateSource]:
    """The day ``row`` sorts on (``YYYY-MM-DD``) and where it came from.

    Args:
        row: A ``MemoryResponse`` (or anything with ``event_start``,
            ``observed_at`` and ``created_at``).

    Returns:
        ``(day, "event" | "observed" | "created")``.
    """
    event_start = getattr(row, "event_start", None)
    if event_weekday(event_start) is not None:
        return event_start, "event"
    observed_at = getattr(row, "observed_at", None)
    # A stored observed_at begins with its UTC day (a date, or a UTC datetime);
    # a legacy value that does not falls through to created_at.
    if observed_weekday(observed_at) is not None:
        return observed_at[:10], "observed"
    created = to_utc_iso(row.created_at)
    return (created or "")[:10], "created"


def date_ordered(result: Any) -> Any:
    """``result`` (a ``RecallResponse``) with its rows dated and oldest first.

    Returns a copy: the rows are copied with ``date`` / ``date_source`` set,
    sorted stably by day, and the response says ``order: "date"``. Every
    other field is kept as it was, ``confidence`` included.
    """
    rows = []
    for row in result.results:
        day, source = row_date(row)
        rows.append(row.model_copy(update={"date": day, "date_source": source}))
    rows.sort(key=lambda row: row.date)
    return result.model_copy(update={"results": rows, "order": "date"})
