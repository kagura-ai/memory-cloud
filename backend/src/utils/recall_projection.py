"""One recall result projection for every surface that serves recall rows (#2013).

``recall`` (MCP) and the recall component of ``get_agent_bootstrap`` (MCP and
REST) render a ``MemoryResponse`` through ``recall_result_item``, so the same
memory has the same keys and the same empty-value representation — absent,
never ``null`` / ``[]`` (#1599) — on both paths at the same verbosity. It lives
in ``utils`` so a service can use it without importing an MCP tool module.
"""

from __future__ import annotations

from typing import Any

from utils.datetime import to_utc_iso

# #2002: recall's ``verbosity`` values. "compact" is the default — a cheap
# first pass an agent can afford without certainty, then reference(memory_id).
RECALL_VERBOSITIES = ("compact", "full")
# Characters of ``summary`` a compact item carries, the ellipsis included.
COMPACT_SUMMARY_CHARS = 160
_ELLIPSIS = "…"


def _compact_summary(summary: str) -> tuple[str, bool]:
    """The head of ``summary`` a compact item carries, and whether text was cut.

    A summary within ``COMPACT_SUMMARY_CHARS`` is returned as stored. A longer
    one is flattened first (every run of whitespace, newlines included, becomes
    one space), then cut at the last space that leaves room for the ellipsis.
    When that space falls in the first half — a URL, path or UUID would
    otherwise collapse the head to a few words — the cut is made mid-token.

    Args:
        summary: The stored summary.

    Returns:
        The head and ``True`` when characters (not just whitespace) were dropped.
    """
    if len(summary) <= COMPACT_SUMMARY_CHARS:
        return summary, False
    flat = " ".join(summary.split())
    if len(flat) <= COMPACT_SUMMARY_CHARS:
        return flat, False
    head = flat[: COMPACT_SUMMARY_CHARS - 1]
    boundary = head.rfind(" ")
    if boundary >= COMPACT_SUMMARY_CHARS // 2:
        head = head[:boundary]
    return head.rstrip() + _ELLIPSIS, True


def _compact_recall_item(r: Any) -> dict[str, Any]:
    """The ``verbosity="compact"`` projection of one recall result (#2002).

    What an agent needs to judge a hit and fetch it: id, the head of the
    summary (``summary_cut: true`` when it was shortened), type, importance,
    score, created_at and updated_at (null on a memory never edited), plus the
    rare annotations it must act on. tags, context_summary and scope stay in
    ``verbosity="full"``.
    """
    summary, cut = _compact_summary(r.summary)
    item: dict[str, Any] = {"memory_id": str(r.memory_id), "summary": summary}
    if cut:
        item["summary_cut"] = True
    item.update(
        {
            "type": r.type,
            "importance": r.importance,
            "score": round(r.score, 4) if r.score is not None else None,
            "created_at": to_utc_iso(r.created_at),
            "updated_at": to_utc_iso(r.updated_at),
        }
    )
    _add_recall_annotations(item, r)
    return item


def _add_recall_annotations(item: dict[str, Any], r: Any) -> None:
    """Copy the fact-succession annotations onto ``item`` when present."""
    # #1208: superseded_by is only set under include_superseded=true;
    # contradicts lists opposing memories (never hidden, both sides annotated).
    if r.superseded_by:
        item["superseded_by"] = str(r.superseded_by)
    if r.contradicts:
        item["contradicts"] = [str(c) for c in r.contradicts]
    # #1403: liveness-guarded near-duplicate this memory may supersede — a
    # client can offer confirm→create_edge.
    if r.supersede_candidate:
        # exclude_none: ``verified`` (#2016) and ``detected_at`` are keys only
        # when they have a value — "absent, never null".
        item["supersede_candidate"] = r.supersede_candidate.model_dump(
            mode="json", exclude_none=True
        )


def recall_result_item(r: Any, verbosity: str = "full") -> dict[str, Any]:
    """Project one recall result for the MCP recall tool and the agent bootstrap.

    Serves the ``recall`` tool's results and the recall component of
    ``get_agent_bootstrap`` (MCP and REST), which uses ``"full"`` and leaves
    out ``supersede_candidate`` (#2013).

    #1599: the annotations that are empty on almost every result are omitted
    rather than serialized as ``null`` / ``[]`` — absence is the signal, as for
    ``persistence`` / ``lint`` on the write tools. A present value is rendered
    exactly as before.

    Args:
        r: A ``MemoryResponse`` from ``RecallResponse.results``.
        verbosity: ``"full"`` (every Layer 1-2 field) or ``"compact"`` (#2002).

    Returns:
        The result item, Layers 1-2 only.
    """
    if verbosity == "compact":
        return _compact_recall_item(r)
    item: dict[str, Any] = {"memory_id": str(r.memory_id), "summary": r.summary}
    # Truthiness, like the annotations below: a stored "" (remember() has no
    # min_length) says as little as None, so it is absent too.
    if r.context_summary:
        item["context_summary"] = r.context_summary
    item.update(
        {
            "type": r.type,
            "importance": r.importance,
            "scope": r.scope,
            # 4 decimals keep the ranking readable; the full float is 16+
            # digits of noise per result.
            "score": round(r.score, 4) if r.score is not None else None,
            # Issue #1047: recency/staleness cues for the agent. created_at
            # is the always-present floor; updated_at is the last real change
            # (null if never edited) — an old value means the fact may be stale.
            "created_at": to_utc_iso(r.created_at),
            "updated_at": to_utc_iso(r.updated_at),
        }
    )
    # #2043: the version to pass as update_memory's expected_version. Full
    # only: compact is the judge-and-fetch pass, and an edit reads the memory
    # (reference, which carries it) first. Absent when the row did not say.
    if r.version is not None:
        item["version"] = r.version
    # #1851: an untagged result says nothing with ``"tags": []``; absent like
    # the other empty annotations.
    if r.tags:
        item["tags"] = r.tags
    _add_recall_annotations(item, r)
    return item
