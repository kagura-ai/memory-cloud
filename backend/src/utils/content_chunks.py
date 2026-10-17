"""Content chunk vectors for dense retrieval (#2015).

A memory's dense vector is built from its ``summary`` only; ``content``
reaches retrieval only through BM25. For a context that opts in
(``ContextSearchConfig.content_chunks_enabled``), a long ``content`` is also
embedded as fixed-size chunks, stored as extra Qdrant points in the same
collection and the same ``dense`` named vector. A chunk point:

- has a deterministic id (``chunk_point_id``) that is never a ``Memory.id``;
- carries ``chunk_of`` (the memory id) — its presence is what marks a chunk
  point, like ``resource_id`` marks a resource point;
- copies every filterable payload field of the memory's summary point, so all
  search filters and the isolation fields apply to it unchanged;
- carries no ``bm25`` sparse vector and none of the BM25 token fields (BM25
  already sees ``content[:2000]`` through the summary point).

Dense searches exclude chunk points by default (``include_chunks=False`` in
``db.qdrant.search_memories_qdrant``), so every caller that assumes one point
per memory keeps working. Recall opts in and collapses the hits back to one
per memory (``collapse_chunk_hits``) before fusion and rerank.

This module is pure: no I/O, no settings.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid5

#: Payload key that marks a chunk point and names its memory.
CHUNK_OF_FIELD = "chunk_of"

#: Fixed chunk size and overlap, in characters.
CONTENT_CHUNK_CHARS = 1200
CONTENT_CHUNK_OVERLAP_CHARS = 200

#: Content shorter than this is fully covered by BM25's ``content[:2000]`` and
#: by the summary vector's neighbourhood; no chunks are written.
CONTENT_CHUNK_MIN_CONTENT_CHARS = 1500

#: Upper bound on chunk points per memory (the tail past it is not embedded).
CONTENT_CHUNK_MAX_PER_MEMORY = 16

#: Dense-arm oversampling on a chunk-enabled context: several hits of one
#: memory collapse into one, so the arm fetches more before collapsing.
CONTENT_CHUNK_DENSE_OVERSAMPLE = 3
CONTENT_CHUNK_DENSE_MAX_FETCH = 600

#: How much of the matched chunk the reranker sees, after the summary.
RERANK_CHUNK_TEXT_MAX_CHARS = 1500

# Fixed namespace for chunk point ids (never change: ids must stay stable so a
# re-embed overwrites the same points).
_CHUNK_NAMESPACE = UUID("6f1c2a4e-2015-4c0b-9a5e-c0a7e1c4b1d5")

# BM25-only payload fields of a summary point; chunk points have no sparse
# vector, so carrying them would only grow the payload.
_TOKEN_FIELDS = frozenset(
    {"summary_tokens", "context_summary_tokens", "content_tokens", "summary_reading"}
)

# Chunk-only payload fields, removed when a collapsed hit falls back to a
# chunk point's payload.
_CHUNK_FIELDS = (CHUNK_OF_FIELD, "chunk_index", "chunk_text")


def chunk_payload_updates(payload_updates: dict[str, Any]) -> dict[str, Any]:
    """The part of a summary-point payload update that applies to chunk points."""
    return {k: v for k, v in payload_updates.items() if k not in _TOKEN_FIELDS}


def split_content(content: str | None) -> list[str]:
    """Split ``content`` into fixed-size overlapping chunks.

    Returns ``[]`` when the content is shorter than
    ``CONTENT_CHUNK_MIN_CONTENT_CHARS``. At most
    ``CONTENT_CHUNK_MAX_PER_MEMORY`` chunks are returned.
    """
    if not content or len(content) < CONTENT_CHUNK_MIN_CONTENT_CHARS:
        return []
    step = CONTENT_CHUNK_CHARS - CONTENT_CHUNK_OVERLAP_CHARS
    chunks: list[str] = []
    start = 0
    while start < len(content) and len(chunks) < CONTENT_CHUNK_MAX_PER_MEMORY:
        chunks.append(content[start : start + CONTENT_CHUNK_CHARS])
        if start + CONTENT_CHUNK_CHARS >= len(content):
            break
        start += step
    return chunks


def expected_chunk_count(content_length: int) -> int:
    """How many chunks ``split_content`` makes of a content this long.

    Lets a caller that only has ``char_length(content)`` (the embedding
    migration's verify) know how many chunk points a memory should have,
    without loading the content.
    """
    if content_length < CONTENT_CHUNK_MIN_CONTENT_CHARS:
        return 0
    step = CONTENT_CHUNK_CHARS - CONTENT_CHUNK_OVERLAP_CHARS
    beyond_first = content_length - CONTENT_CHUNK_CHARS
    return min(CONTENT_CHUNK_MAX_PER_MEMORY, 1 + -(-beyond_first // step))


def chunk_point_id(memory_id: UUID | str, chunk_index: int) -> str:
    """Deterministic Qdrant point id of a memory's ``chunk_index``-th chunk."""
    return str(uuid5(_CHUNK_NAMESPACE, f"{memory_id}:{chunk_index}"))


def build_chunk_payload(
    base_payload: dict[str, Any],
    *,
    memory_id: UUID | str,
    chunk_index: int,
    chunk_text: str,
) -> dict[str, Any]:
    """Payload of a chunk point, derived from the memory's summary-point payload.

    Keeps every filterable field (and ``summary`` / ``context_summary``, which
    the reranker reads), drops the BM25 token fields, and adds the chunk
    fields. ``base_payload`` is not modified.
    """
    payload = {k: v for k, v in base_payload.items() if k not in _TOKEN_FIELDS}
    payload[CHUNK_OF_FIELD] = str(memory_id)
    payload["chunk_index"] = chunk_index
    payload["chunk_text"] = chunk_text
    return payload


def collapse_chunk_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse dense hits to one per memory, by max score.

    Each output hit has the memory id as ``id``, the group's best score, and
    the summary point's ``payload`` and ``embedding`` when the summary point is
    in the group (else the best chunk's payload without the chunk fields, and
    an empty embedding). ``matched_chunk_text`` / ``matched_chunk_index`` are
    set when a chunk point scored best, else ``None``. Output is sorted by
    score, highest first. Hits without any chunk point are returned unchanged.

    Known PR-1 limit: a memory found only through its chunks has an empty
    ``embedding``, so recall's Hebbian co-activation skips it (a chunk vector
    embeds other text than the summary vectors it would be compared with).
    """
    if not any(CHUNK_OF_FIELD in (h.get("payload") or {}) for h in hits):
        return hits

    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for hit in hits:
        payload = hit.get("payload") or {}
        chunk_of = payload.get(CHUNK_OF_FIELD)
        key = str(chunk_of) if chunk_of else str(hit["id"])
        group = groups.get(key)
        if group is None:
            group = {"summary": None, "best": None}
            groups[key] = group
            order.append(key)
        if not chunk_of:
            group["summary"] = hit
        best = group["best"]
        if best is None or hit["score"] > best["score"]:
            group["best"] = hit

    out: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        best = group["best"]
        summary_hit = group["summary"]
        best_payload = best.get("payload") or {}
        chunk_won = CHUNK_OF_FIELD in best_payload
        if summary_hit is not None:
            base = summary_hit
            payload = summary_hit.get("payload")
            embedding = summary_hit.get("embedding", [])
        else:
            base = best
            payload = {k: v for k, v in best_payload.items() if k not in _CHUNK_FIELDS}
            embedding = []
        collapsed = dict(base)
        collapsed["id"] = summary_hit["id"] if summary_hit is not None else key
        collapsed["score"] = best["score"]
        collapsed["payload"] = payload
        collapsed["embedding"] = embedding
        collapsed["matched_chunk_text"] = best_payload.get("chunk_text") if chunk_won else None
        collapsed["matched_chunk_index"] = best_payload.get("chunk_index") if chunk_won else None
        out.append(collapsed)
    out.sort(key=lambda h: h["score"], reverse=True)
    return out


def rerank_document(candidate: dict[str, Any]) -> str:
    """Text the reranker scores for one recall candidate.

    ``"{summary} {context_summary}"`` as before (byte-identical when no chunk
    matched), followed by the matched chunk's text when a chunk point is what
    found the memory — put after the summary so a provider that truncates the
    document never cuts the summary.
    """
    payload = candidate["payload"]
    doc = f"{payload['summary']} {payload.get('context_summary', '')}"
    chunk_text = candidate.get("matched_chunk_text")
    if chunk_text:
        doc = f"{doc}\n{chunk_text[:RERANK_CHUNK_TEXT_MAX_CHARS]}"
    return doc
