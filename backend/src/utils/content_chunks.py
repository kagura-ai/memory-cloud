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

import unicodedata
from typing import Any
from uuid import UUID, uuid5

import xxhash

from utils.text import normalize_for_search

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

#: The longest document the reranker sees for a payload without a ``summary``
#: (a resource-indexed point), lead fields, context summary and matched chunk
#: included.
RERANK_RESOURCE_DOC_MAX_CHARS = 1500

#: Fields of a resource-indexed point whose line of the indexed text is put
#: first in its rerank document, in this order. The indexed text lists fields
#: in schema order (by name for the sync feed schema), so a long ``brand`` or
#: ``answer`` would otherwise push a product's title and sku or an FAQ's
#: question past the cap. The value is read from the point's ``metadata``; the
#: line is moved only when the indexed text holds it, so the document never
#: carries a field the indexed text does not.
RERANK_RESOURCE_LEAD_FIELDS = ("title", "question", "sku")

#: Room in a resource rerank document always left to a context summary the
#: user wrote or edited (it goes after the indexed text): at most half the cap,
#: so a long summary never pushes the indexed text out, nor a long text the
#: summary. A short indexed text leaves it the rest of the cap.
RERANK_RESOURCE_USER_SUMMARY_ROOM = RERANK_RESOURCE_DOC_MAX_CHARS // 2

#: The longest ``context_summary`` the resource indexer writes for a document
#: (see :func:`resource_context_summary`).
RESOURCE_CONTEXT_SUMMARY_MAX_CHARS = 2000

#: Stand-in document for a candidate whose payload carries no text at all, so
#: every candidate gets a non-empty document.
RERANK_EMPTY_DOCUMENT = "(no text)"

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
_CHUNK_FIELDS = (CHUNK_OF_FIELD, "chunk_index", "chunk_text", "content_hash")


def chunk_payload_updates(payload_updates: dict[str, Any]) -> dict[str, Any]:
    """The part of a summary-point payload (or payload update) that chunk points copy.

    Every field but the BM25 token fields. ``build_chunk_payload`` starts from
    it, and the Sleep reindex applies it to a memory's existing chunk points
    when only the metadata changed.
    """
    return {k: v for k, v in payload_updates.items() if k not in _TOKEN_FIELDS}


def content_hash(content: str | None) -> str:
    """Hash of the content a memory's chunks were split from (``content_hash`` payload).

    xxh64 of the NFC-normalized text, the embedding cache's normalization.
    Written on every chunk point so a later writer can tell whether the
    chunks it would write are already there; nothing reads it yet.
    """
    normalized = unicodedata.normalize("NFC", content or "")
    return xxhash.xxh64(normalized.encode()).hexdigest()


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
    content_hash: str | None = None,
) -> dict[str, Any]:
    """Payload of a chunk point, derived from the memory's summary-point payload.

    Keeps every filterable field (and ``summary`` / ``context_summary``, which
    the reranker reads), drops the BM25 token fields, and adds the chunk
    fields (``content_hash`` when given, see :func:`content_hash`).
    ``base_payload`` is not modified.
    """
    payload = chunk_payload_updates(base_payload)
    payload[CHUNK_OF_FIELD] = str(memory_id)
    payload["chunk_index"] = chunk_index
    payload["chunk_text"] = chunk_text
    if content_hash is not None:
        payload["content_hash"] = content_hash
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


def resource_context_summary(content: str) -> str:
    """The ``context_summary`` the resource indexer writes for a document.

    Its indexed text, cut to ``RESOURCE_CONTEXT_SUMMARY_MAX_CHARS`` with an
    ellipsis. The point stores it ``normalize_for_search``-ed.
    """
    if len(content) > RESOURCE_CONTEXT_SUMMARY_MAX_CHARS:
        return content[: RESOURCE_CONTEXT_SUMMARY_MAX_CHARS - 3] + "..."
    return content


def _is_indexer_context_summary(context_summary: str, content: str) -> bool:
    """Whether ``context_summary`` is the indexer's own cut of ``content``."""
    cut = resource_context_summary(content)
    if context_summary == cut:
        return True
    try:
        return context_summary == normalize_for_search(cut)
    except ValueError:  # over MAX_NORMALIZE_SIZE: keep the summary
        return False


def _take_lead_line(text: str, name: str, value: str) -> tuple[str | None, str]:
    """Take the indexed-text line of field ``name`` (value ``value``) out of ``text``.

    The indexed text writes a field as ``<label>: <value>``, the label being
    the field's description when the schema gives one, else its name; a value
    can span lines. Returns ``(line, rest)`` when exactly one line is labelled
    ``name`` or, failing that, exactly one line of any label carries the value;
    else ``(None, text)`` (the field is not in the indexed text, or which line
    is its own is ambiguous). In that fallback a line whose label part holds
    ``": "`` is no field's line (it sits inside another field's multi-line
    value) and is not counted.
    """
    needle = f": {value}"
    spans: list[tuple[int, int, str]] = []
    at = text.find(needle)
    while at != -1:
        end = at + len(needle)
        if end == len(text) or text[end] == "\n":
            start = text.rfind("\n", 0, at) + 1
            if start < at:
                spans.append((start, end, text[start:at]))
        at = text.find(needle, at + 1)
    named = [span for span in spans if span[2] == name]
    labelled = [span for span in spans if ": " not in span[2]]
    picked = named if len(named) == 1 else labelled if len(labelled) == 1 else []
    if not picked:
        return None, text
    start, end, _label = picked[0]
    before = text[: max(start - 1, 0)]
    after = text[end + 1 :]
    rest = f"{before}\n{after}" if before and after else before or after
    return text[start:end], rest


def _resource_rerank_document(payload: dict[str, Any], chunk: str = "") -> str:
    """Rerank document of a payload without a ``summary`` (a resource-indexed point).

    The lines of the lead fields first (``RERANK_RESOURCE_LEAD_FIELDS``, moved
    out of ``content`` when it holds them, see :func:`_take_lead_line`), then
    the rest of ``content``, then the ``context_summary`` unless it is the
    indexer's own cut of ``content`` (kept when the user wrote or edited it),
    then the matched ``chunk`` text, one line break between the parts.

    Fits ``RERANK_RESOURCE_DOC_MAX_CHARS``. A kept context summary always gets
    ``RERANK_RESOURCE_USER_SUMMARY_ROOM`` (all of it when shorter), the chunk
    the rest it needs, and the indexed text what is left; it is the indexed
    text that is cut, so a long text never pushes the summary or the chunk
    out. Room the indexed text does not take goes to the summary.
    """
    content = payload.get("content")
    content = content if isinstance(content, str) else ""
    rest = content
    parts: list[str] = []
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for name in RERANK_RESOURCE_LEAD_FIELDS:
            value = metadata.get(name)
            if isinstance(value, bool) or not isinstance(value, str | int | float):
                continue
            if isinstance(value, str) and not value.strip():
                continue
            line, rest = _take_lead_line(rest, name, str(value))
            if line is not None:
                parts.append(line)
    parts.append(rest)
    body = "\n".join(p for p in parts if p.strip())
    context_summary = payload.get("context_summary")
    if (
        not isinstance(context_summary, str)
        or not context_summary.strip()
        or _is_indexer_context_summary(context_summary, content)
    ):
        context_summary = ""
    cap = RERANK_RESOURCE_DOC_MAX_CHARS
    # Room of the user summary first, then of the chunk; each part after the
    # first costs one more character for its line break.
    summary_room = min(len(context_summary), RERANK_RESOURCE_USER_SUMMARY_ROOM)
    taken = summary_room + 1 if summary_room else 0
    chunk = chunk[: max(cap - taken, 0)]
    taken += len(chunk) + 1 if chunk else 0
    body = body[: max(cap - taken, 0)]
    taken = len(body) + 1 if body else 0
    taken += len(chunk) + 1 if chunk else 0
    kept = context_summary[: max(cap - taken, 0)]
    return "\n".join(part for part in (body, kept, chunk) if part)


def rerank_document(candidate: dict[str, Any]) -> str:
    """Text the reranker scores for one recall candidate.

    ``"{summary} {context_summary}"`` as before (byte-identical when no chunk
    matched), followed by the matched chunk's text when a chunk point is what
    found the memory — put after the summary so a provider that truncates the
    document never cuts the summary.

    No payload key is required (#3031): a payload without a ``summary`` (the
    point the resource indexer writes) is scored on its ``content`` with the
    lead fields' lines first, then a context summary the user wrote, then the
    matched chunk (:func:`_resource_rerank_document`), the whole document
    within ``RERANK_RESOURCE_DOC_MAX_CHARS``: the indexed text is what gets
    cut, so the user's summary and the chunk keep their room. A candidate with
    no text at all gets ``RERANK_EMPTY_DOCUMENT``, so one candidate never fails
    the whole pool and no document is empty.
    """
    payload = candidate.get("payload") or {}
    summary = payload.get("summary")
    chunk_text = candidate.get("matched_chunk_text")
    cut = chunk_text[:RERANK_CHUNK_TEXT_MAX_CHARS] if chunk_text else ""
    if isinstance(summary, str) and summary:
        # The memory point always carries the key; its value is None when the
        # memory has no context summary.
        context_summary = payload.get("context_summary")
        doc = f"{summary} {context_summary if isinstance(context_summary, str) else ''}"
        if cut:
            doc = f"{doc}\n{cut}"
    else:
        doc = _resource_rerank_document(payload, cut)
    if not doc.strip():
        return RERANK_EMPTY_DOCUMENT
    return doc
