"""Rejection half of the supersede-suggestion lifecycle (Issue #1504).

Accepting a ``supersede_candidate`` is one ``create_edge(..., "supersedes")``
call, and the edge itself both records the decision and stops the suggestion
resurfacing (the detector skips a pair that already has the edge, and acceptance
clears the stored value). There was no equivalent for the opposite judgement.

When an agent decides two memories are deliberately SEPARATE — the same
conclusion held at two altitudes, say, rather than one superseding the other —
the suggestion returned on every ``recall()`` and ``reference()`` touching that
memory, indefinitely, across sessions. The only ways to silence it were a wrong
accept (which shadows a memory that should stay live) or deleting one of them.

This adds a tombstone, stored in the same server-only ``supersede_candidate``
column so no migration is needed and no client-writable surface is involved:

    {"dismissed": {"memory_id": ..., "similarity": ..., "dismissed_at": ...}}

Two properties follow from that shape:

* It carries no top-level ``memory_id``/``similarity``, so
  ``_resolve_supersede_candidates`` — which requires both — skips it without
  needing to know tombstones exist. Dismissed suggestions simply stop surfacing.
* It records the similarity AT dismissal, which is what makes re-detection
  conditional rather than permanent. See :func:`is_dismissed`.
"""

from __future__ import annotations

from typing import Any

from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# How far the recomputed similarity must move before a dismissed pair is
# reconsidered. The issue asks for re-detection when "either memory's embedding
# changes materially" — clearing the tombstone on ANY re-embed would not do
# that, because process_pending_embedding also runs for sleep reindexes and
# backfills, which recompute the SAME vector and would resurrect every dismissal
# the first night. Keying on the similarity delta separates the two: a
# mechanical reindex reproduces the score and stays suppressed, while a real
# content edit moves it and gets a fresh judgement.
RESURFACE_SIMILARITY_DELTA = 0.02


def build_tombstone(candidate: dict[str, Any]) -> dict[str, Any]:
    """Tombstone value for a rejected suggestion.

    Args:
        candidate: The stored suggestion being rejected — must carry
            ``memory_id``; ``similarity`` is recorded when present so
            re-detection can compare against it.

    Returns:
        The value to store in ``Memory.supersede_candidate``.
    """
    dismissed: dict[str, Any] = {
        "memory_id": str(candidate["memory_id"]),
        "dismissed_at": to_utc_iso(utcnow()),
    }
    similarity = candidate.get("similarity")
    if isinstance(similarity, (int, float)):
        dismissed["similarity"] = float(similarity)
    return {"dismissed": dismissed}


def dismissed_entry(stored: Any) -> dict[str, Any] | None:
    """The tombstone inside a stored column value, or None."""
    if not isinstance(stored, dict):
        return None
    entry = stored.get("dismissed")
    return entry if isinstance(entry, dict) else None


def is_dismissed(stored: Any, *, target_id: str, similarity: float) -> bool:
    """Whether a freshly-detected pair was already rejected and still is.

    Args:
        stored: Current ``Memory.supersede_candidate`` value.
        target_id: The newly-detected candidate's memory id.
        similarity: The newly-computed similarity.

    Returns:
        True when this exact pair was dismissed and the similarity has not moved
        by at least ``RESURFACE_SIMILARITY_DELTA`` since. A dismissal of a
        DIFFERENT target never suppresses a new one — rejecting one pairing says
        nothing about another.
    """
    entry = dismissed_entry(stored)
    if entry is None or entry.get("memory_id") != target_id:
        return False

    prior = entry.get("similarity")
    if not isinstance(prior, (int, float)):
        # Pre-delta tombstone (or a malformed one): keep it suppressed. A
        # dismissal is a deliberate judgement, so the safe reading of a missing
        # baseline is "still rejected", not "resurface".
        return True
    return abs(similarity - float(prior)) < RESURFACE_SIMILARITY_DELTA
