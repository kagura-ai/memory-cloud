"""Embedded LanceDB vector store — the "Kagura Lite" backend (PREVIEW).

Activated by ``vector_backend=lance`` (env ``KAGURA_VECTOR_BACKEND=lance``).
Targets single-process self-hosted / CLI / desktop / edge deployments where
running a separate Qdrant server is undesirable. The server (multi-process +
nightly Sleep writer) deployment should stay on Qdrant — LanceDB writes are
single-process and this backend is not built for concurrent multi-writer use.

Design (mirrors the Qdrant data path so no higher layer changes):

* **Japanese stays owned by Sudachi.** Documents are indexed on a single
  weighted ``search_text`` column built from the *already Sudachi-tokenized*
  payload fields (``summary_tokens`` ×2, ``context_summary_tokens`` ×2,
  ``content_tokens`` ×1, plus ``summary_reading`` for the hiragana fallback).
  Field weighting is expressed as token repetition (higher TF). Queries are
  tokenized through the exact same pipeline as ``search_memories_fulltext``
  (lemmas + readings + augmentation + synonym expansion), so Japanese recall
  behavior matches the Qdrant path without relying on LanceDB's native CJK
  tokenizer.
* **Hybrid fusion stays in services/search_service.py.** This store returns
  semantic-only and BM25-only results in the same shape as the Qdrant
  functions; the 60/40 fusion is unchanged at the call layer.
* **Isolation is a SQL ``WHERE`` string** (``build_lance_filter``) used as a
  LanceDB pre-filter. ``workspace_id`` / ``context_id`` are UUID-validated;
  ``user_id`` is an OAuth2 sub (not a UUID). Every embedded literal is
  single-quote escaped, so no value can break out of its string context.

PREVIEW limitations (documented, not silently dropped):

* ``copy_context_points`` / ``delete_user_points`` (GDPR cross-collection) /
  the admin BM25-drift scroll are not implemented for this backend and raise
  :class:`NotImplementedError`.
* Cosine ``_distance`` is converted to a similarity score via ``1 - distance``.
* End-to-end behavior requires ``lancedb`` installed (``pip install
  'kagura-memory[lite]'``) and is pending live validation; the SQL filter
  builder is unit-tested independently of LanceDB.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC
from typing import TYPE_CHECKING, Any
from uuid import UUID

from utils.datetime import parse_iso8601_to_aware, to_utc_iso
from utils.exceptions import QdrantError
from utils.logger import get_logger
from utils.tokenizer import build_fulltext_query, tokenize_for_search

if TYPE_CHECKING:  # pragma: no cover - typing only
    import lancedb

logger = get_logger(__name__)

DEFAULT_COLLECTION = "kagura_memories"

# Field weights for the combined FTS column, expressed as token repetition.
# Mirrors utils/sparse_vector.build_document_sparse_vector (summary 2.0 /
# context_summary 2.0 / content 1.0, plus the reading fallback).
_FIELD_REPEATS: tuple[tuple[str, str, int], ...] = (
    ("summary_tokens", "summary", 2),
    ("context_summary_tokens", "context_summary", 2),
    ("content_tokens", "content", 1),
)
_MAX_TAGS = 50


# ---------------------------------------------------------------------------
# SQL filter builder (pure Python, unit-tested without LanceDB)
# ---------------------------------------------------------------------------
def _validate_uuid(value: str, field_name: str) -> None:
    """Reject non-UUID isolation values before they reach a SQL string.

    Mirrors db.qdrant._validate_uuid_format. Defense-in-depth alongside quote
    escaping: isolation keys are always UUIDs, so anything else is rejected.
    """
    from config.constants import ERROR_MSG_INVALID_UUID

    if not value or not isinstance(value, str):
        raise ValueError(ERROR_MSG_INVALID_UUID.format(field=field_name, value=value or "(empty)"))
    try:
        UUID(value)
    except (ValueError, AttributeError) as e:
        raise ValueError(ERROR_MSG_INVALID_UUID.format(field=field_name, value=value)) from e


def _sql_str(value: str) -> str:
    """Quote a string literal for a LanceDB/DataFusion SQL filter.

    Single quotes are doubled per SQL string-literal rules. Isolation values
    are UUID-validated upstream, but every embedded literal is escaped here so
    metadata filter values (scope/type/tags/dates) cannot break out either.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _build_tag_clause(filters: dict[str, Any]) -> str:
    """Build a list-membership clause for tag filtering (Issue #67 / #79)."""
    raw = filters.get("tags")
    if not isinstance(raw, list) or not raw:
        return ""
    valid = [t for t in raw if isinstance(t, str) and t][:_MAX_TAGS]
    if not valid:
        return ""
    mode = filters.get("tags_match", "any")
    if mode not in ("any", "all"):
        raise ValueError(f"Invalid tags_match value: {mode!r}. Must be 'any' or 'all'.")
    clauses = [f"array_has(tags, {_sql_str(t)})" for t in valid]
    joiner = " AND " if mode == "all" else " OR "
    return "(" + joiner.join(clauses) + ")"


def _build_date_clauses(filters: dict[str, Any]) -> list[str]:
    """Build ISO-8601 string comparison clauses for date range filtering (#78)."""
    mapping = {
        "created_after": ("created_at", ">="),
        "created_before": ("created_at", "<="),
        "updated_after": ("updated_at", ">="),
        "updated_before": ("updated_at", "<="),
    }
    out: list[str] = []
    for key, (col, op) in mapping.items():
        value = filters.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"{key} must be an ISO 8601 datetime string, got {type(value).__name__}"
            )
        # Normalize to canonical UTC '...Z' (validates the input, converts any
        # offset to UTC, and matches the to_utc_iso format stored in
        # created_at/updated_at) so the lexical string comparison orders
        # chronologically. Mirrors the Qdrant path's parse_iso8601_to_aware.
        normalized = to_utc_iso(parse_iso8601_to_aware(value, key).astimezone(UTC))
        out.append(f"{col} {op} {_sql_str(normalized)}")
    return out


def build_lance_filter(
    workspace_id: str,
    context_id: str | list[str],
    user_id: str,
    is_shared_context: bool = False,
    filters: dict[str, Any] | None = None,
) -> str:
    """Build the LanceDB pre-filter SQL ``WHERE`` body (no ``WHERE`` keyword).

    Equivalent of db.qdrant._build_search_filter for the SQL-string backend.
    Always emits 3-level isolation (workspace + context + user, the last
    skipped for shared contexts), then optional scope/type/importance/tags/date.

    ``workspace_id`` and ``context_id`` are UUID-validated (they are always
    UUIDs). ``user_id`` is the OAuth2 ``sub`` (e.g. ``google-oauth2|123``) and
    is NOT a UUID — like the Qdrant path it is single-quote escaped via
    ``_sql_str``, not UUID-validated. Every embedded literal is escaped, so no
    value can break out of its string context.

    Args:
        workspace_id: Workspace UUID (isolation).
        context_id: Single context UUID or list of UUIDs (Issue #81).
        user_id: User OAuth2 sub (isolation; omitted for shared contexts).
        is_shared_context: If True, omit the user_id clause.
        filters: Optional scope/type/importance/tags/date metadata filters.

    Returns:
        SQL ``WHERE`` body string (without the ``WHERE`` keyword).

    Raises:
        ValueError: If workspace_id/context_id is not a valid UUID, or a
            metadata filter value is malformed (mirrors the Qdrant 4xx path).
    """
    _validate_uuid(workspace_id, "workspace_id")
    parts = [f"workspace_id = {_sql_str(workspace_id)}"]

    if isinstance(context_id, list):
        for cid in context_id:
            _validate_uuid(cid, "context_id")
        joined = ", ".join(_sql_str(c) for c in context_id)
        parts.append(f"context_id IN ({joined})")
    else:
        _validate_uuid(context_id, "context_id")
        parts.append(f"context_id = {_sql_str(context_id)}")

    if not is_shared_context:
        parts.append(f"user_id = {_sql_str(user_id)}")

    if filters:
        if "scope" in filters:
            parts.append(f"scope = {_sql_str(filters['scope'])}")
        if "type" in filters:
            parts.append(f"type = {_sql_str(filters['type'])}")
        imp = filters.get("importance")
        if isinstance(imp, dict):
            # Mirror db.qdrant._build_importance_range_condition: reject an
            # inconsistent range up-front (4xx) instead of silently emitting an
            # unsatisfiable predicate that returns an empty set.
            if "gte" in imp and "lte" in imp and imp["gte"] > imp["lte"]:
                raise ValueError(f"gte ({imp['gte']}) cannot be greater than lte ({imp['lte']})")
            if "gt" in imp and "lt" in imp and imp["gt"] >= imp["lt"]:
                raise ValueError(f"gt ({imp['gt']}) must be less than lt ({imp['lt']})")
            for op, sql in (("gte", ">="), ("lte", "<="), ("gt", ">"), ("lt", "<")):
                if op in imp:
                    parts.append(f"importance {sql} {float(imp[op])}")
        tag_clause = _build_tag_clause(filters)
        if tag_clause:
            parts.append(tag_clause)
        parts.extend(_build_date_clauses(filters))

    return " AND ".join(parts)


def _build_search_text(payload: dict[str, Any]) -> str:
    """Build the weighted, Sudachi-tokenized FTS text for a document."""
    chunks: list[str] = []
    for token_key, raw_key, repeat in _FIELD_REPEATS:
        tokens = payload.get(token_key) or tokenize_for_search(str(payload.get(raw_key, "") or ""))
        if not tokens:
            continue
        # Token fields are normally space-joined strings; coerce defensively so
        # a list-valued field can never raise TypeError in the join below.
        if not isinstance(tokens, str):
            tokens = " ".join(map(str, tokens)) if isinstance(tokens, list | tuple) else str(tokens)
        chunks.extend([tokens] * repeat)
    reading = payload.get("summary_reading") or ""
    if reading:
        chunks.append(str(reading))
    return " ".join(chunks)


# ---------------------------------------------------------------------------
# LanceDB-backed store
# ---------------------------------------------------------------------------
class LanceVectorStore:
    """In-process LanceDB implementation of the VectorStore Protocol (preview).

    LanceDB's Python client is synchronous; blocking calls are offloaded with
    :func:`asyncio.to_thread` so the async request path is never blocked (the
    accepted bridge for a sync third-party lib — distinct from the forbidden
    sync-SQLAlchemy-in-async pattern).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: lancedb.DBConnection | None = None
        self._tables: dict[str, Any] = {}
        # Per-collection embedding dimension, used when a read/delete must open
        # a table that has to be created. Tracked per collection_name because
        # the default collection (512) and per-model variants (e.g.
        # _voyage_2_1024) have different dims. Set by ensure_collection() /
        # add_memory().
        self._dims: dict[str, int] = {}
        # Collections whose FTS + scalar indexes have been built this process.
        # Indexes are (re)built lazily after the first write, since an empty
        # table cannot be indexed on some LanceDB versions.
        self._indexed: set[str] = set()
        # Serializes lazy connect/table creation AND every write: _run closures
        # run in the asyncio.to_thread pool and LanceDB is not safe for
        # concurrent writers even in-process. RLock because the write path
        # re-enters via _open(). Reads (MVCC) do not take it.
        self._lock = threading.RLock()

    # -- connection / table helpers (run inside to_thread) ------------------
    def _connect(self) -> Any:
        with self._lock:
            if self._db is None:
                import lancedb

                self._db = lancedb.connect(self._db_path)
            return self._db

    @staticmethod
    def _schema(dim: int) -> Any:
        import pyarrow as pa

        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
                pa.field("workspace_id", pa.string()),
                pa.field("context_id", pa.string()),
                pa.field("user_id", pa.string()),
                pa.field("scope", pa.string()),
                pa.field("type", pa.string()),
                pa.field("importance", pa.float32()),
                pa.field("tags", pa.list_(pa.string())),
                pa.field("created_at", pa.string()),
                pa.field("updated_at", pa.string()),
                pa.field("search_text", pa.string()),
                pa.field("payload_json", pa.string()),
            ]
        )

    def _open(self, collection_name: str, dim: int = 0) -> Any:
        """Open (creating if needed) a collection table, or return ``None``.

        ``dim`` is only consulted when the table must be created (write path —
        add_memory / ensure_collection pass a real dim). Read/delete callers
        pass ``0``: if the table does not exist yet they get ``None`` (an empty
        store ⇒ no rows / nothing to delete), never a zero-width vector column.
        Per-collection dims are tracked so a variant collection is created at
        its own dimension, not another collection's.
        """
        with self._lock:
            cached = self._tables.get(collection_name)
            if cached is not None:
                return cached
            db = self._connect()
            if collection_name in db.table_names():
                tbl = db.open_table(collection_name)
            else:
                effective_dim = dim or self._dims.get(collection_name, 0)
                if effective_dim <= 0:
                    # Read/delete on a not-yet-created collection → empty store.
                    return None
                tbl = db.create_table(collection_name, schema=self._schema(effective_dim))
            self._tables[collection_name] = tbl
            return tbl

    def _ensure_indexes(self, tbl: Any) -> None:
        """Best-effort (re)build of the scalar isolation + FTS indexes.

        Called from ensure_collection and lazily after the first write, because
        some LanceDB versions cannot build an index on an empty table. The table
        is fully queryable without them (full scan); once built, LanceDB folds
        later rows in, so building once per collection is sufficient.
        """
        for col in ("context_id", "workspace_id", "user_id"):
            try:
                tbl.create_scalar_index(col, replace=True)
            except Exception as e:  # noqa: BLE001 - best-effort preview index
                logger.debug("lance_scalar_index_deferred", column=col, error=str(e))
        try:
            tbl.create_fts_index("search_text", use_tantivy=False, replace=True)
        except Exception as e:  # noqa: BLE001 - FTS index needs rows on some versions
            logger.warning("lance_fts_index_deferred", error=str(e))

    # -- VectorStore Protocol ----------------------------------------------
    async def ensure_collection(
        self, embedding_dim: int = 512, collection_name: str = DEFAULT_COLLECTION
    ) -> None:
        if embedding_dim > 0:
            self._dims[collection_name] = embedding_dim

        def _run() -> None:
            with self._lock:
                tbl = self._open(collection_name, embedding_dim)
                self._ensure_indexes(tbl)
                self._indexed.add(collection_name)

        try:
            await asyncio.to_thread(_run)
            logger.info("lance_collection_ready", collection=collection_name, dim=embedding_dim)
        except Exception as e:
            raise QdrantError(f"Failed to ensure LanceDB collection: {e}") from e

    async def add_memory(
        self,
        user_id: str,
        memory_id: UUID,
        vector: list[float],
        payload: dict[str, Any],
        workspace_id: str,
        context_id: str,
        sparse_indices: list[int] | None = None,
        sparse_values: list[float] | None = None,
        collection_name: str = DEFAULT_COLLECTION,
    ) -> None:
        if not workspace_id or not context_id or not user_id:
            raise ValueError(
                "workspace_id, context_id, and user_id are required. "
                f"Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
            )
        _validate_uuid(workspace_id, "workspace_id")
        _validate_uuid(context_id, "context_id")

        self._dims[collection_name] = len(vector)

        # Mirror the Qdrant writer: isolation keys live in the payload too.
        payload = {
            **payload,
            "workspace_id": workspace_id,
            "context_id": context_id,
            "user_id": user_id,
        }
        tags = payload.get("tags")
        importance = payload.get("importance")
        row = {
            "id": str(memory_id),
            "vector": [float(x) for x in vector],
            "workspace_id": workspace_id,
            "context_id": context_id,
            "user_id": user_id,
            "scope": str(payload.get("scope", "") or ""),
            "type": str(payload.get("type", "") or ""),
            "importance": float(importance) if importance is not None else 0.5,
            "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
            "created_at": str(payload.get("created_at", "") or ""),
            "updated_at": str(payload.get("updated_at", "") or ""),
            "search_text": _build_search_text(payload),
            "payload_json": json.dumps(payload, ensure_ascii=False),
        }

        def _run() -> None:
            # Hold the lock across the write: LanceDB is not concurrent-writer
            # safe even in-process, and Sleep/embedding tasks issue overlapping
            # add_memory calls from separate to_thread workers.
            with self._lock:
                tbl = self._open(collection_name, len(vector))
                # Upsert by id so re-embeds (Sleep reindex) overwrite cleanly.
                (
                    tbl.merge_insert("id")
                    .when_matched_update_all()
                    .when_not_matched_insert_all()
                    .execute([row])
                )
                # Build indexes once the table has its first row (deferred from
                # ensure_collection on the empty table).
                if collection_name not in self._indexed:
                    self._ensure_indexes(tbl)
                    self._indexed.add(collection_name)

        try:
            await asyncio.to_thread(_run)
        except Exception as e:
            raise QdrantError(f"Failed to add memory to LanceDB: {e}") from e

    async def search_semantic(
        self,
        user_id: str,
        query_vector: list[float],
        workspace_id: str,
        context_id: str | list[str],
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        is_shared_context: bool = False,
        collection_name: str = DEFAULT_COLLECTION,
        include_vectors: bool = False,
    ) -> list[dict]:
        if not workspace_id or not context_id or not user_id:
            raise ValueError(
                "Isolation requires workspace_id, context_id, and user_id. "
                f"Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
            )
        where = build_lance_filter(workspace_id, context_id, user_id, is_shared_context, filters)

        def _run() -> list[dict]:
            tbl = self._open(collection_name)
            if tbl is None:
                return []
            q = (
                tbl.search(query_vector, vector_column_name="vector")
                .metric("cosine")
                .where(where, prefilter=True)
            )
            return q.limit(limit).to_list()

        try:
            rows = await asyncio.to_thread(_run)
        except Exception as e:
            raise QdrantError(f"LanceDB semantic search failed: {e}") from e

        return [
            {
                "id": row["id"],
                # .metric("cosine") above makes _distance a cosine distance in
                # [0, 2], so 1 - _distance is cosine similarity (matches the
                # Qdrant Distance.COSINE scores). Missing _distance ranks last.
                "score": (
                    (1.0 - float(row["_distance"])) if row.get("_distance") is not None else 0.0
                ),
                "payload": json.loads(row["payload_json"]),
                "embedding": list(row.get("vector") or []) if include_vectors else [],
            }
            for row in rows
        ]

    async def search_fulltext(
        self,
        user_id: str,
        query: str,
        workspace_id: str,
        context_id: str | list[str],
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        is_shared_context: bool = False,
        collection_name: str = DEFAULT_COLLECTION,
    ) -> list[dict]:
        if not workspace_id or not context_id or not user_id:
            raise ValueError(
                "Isolation requires workspace_id, context_id, and user_id. "
                f"Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
            )
        where = build_lance_filter(workspace_id, context_id, user_id, is_shared_context, filters)

        # Shared expanded-query pipeline (identical to
        # db.qdrant.search_memories_fulltext) so both backends tokenize the same.
        expanded = build_fulltext_query(query).strip()
        if not expanded:
            logger.debug("lance_bm25_query_empty_after_tokenization", query=query[:50])
            return []

        def _run() -> list[dict]:
            tbl = self._open(collection_name)
            if tbl is None:
                return []
            q = tbl.search(expanded, query_type="fts").where(where, prefilter=True)
            return q.limit(limit).to_list()

        try:
            rows = await asyncio.to_thread(_run)
        except Exception as e:
            raise QdrantError(f"LanceDB BM25 search failed: {e}") from e

        return [
            {
                "id": row["id"],
                "score": float(row.get("_score", row.get("score", 0.0)) or 0.0),
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    async def update_payload(
        self,
        memory_id: UUID,
        payload_updates: dict[str, Any],
        collection_name: str = DEFAULT_COLLECTION,
    ) -> None:
        mirror_cols = ("scope", "type", "importance", "tags", "created_at", "updated_at")
        where = f"id = {_sql_str(str(memory_id))}"

        def _run() -> None:
            with self._lock:
                tbl = self._open(collection_name)
                if tbl is None:
                    return
                # Read only the payload blob (NOT the vector), then write the
                # changed columns back in place with tbl.update(). update() (vs
                # a full-row merge_insert) guarantees the dense embedding is
                # never dropped even if the read projection omits the vector.
                existing = tbl.search().where(where).select(["payload_json"]).limit(1).to_list()
                if not existing:
                    return
                payload = json.loads(existing[0]["payload_json"])
                payload.update(payload_updates)
                values: dict[str, Any] = {
                    "payload_json": json.dumps(payload, ensure_ascii=False),
                    "search_text": _build_search_text(payload),
                }
                for col in mirror_cols:
                    if col in payload_updates:
                        values[col] = payload_updates[col]
                tbl.update(where=where, values=values)

        try:
            await asyncio.to_thread(_run)
        except Exception as e:
            raise QdrantError(f"Failed to update memory payload in LanceDB: {e}") from e

    async def delete_memory(
        self,
        user_id: str,
        memory_id: UUID,
        collection_name: str = DEFAULT_COLLECTION,
    ) -> None:
        def _run() -> None:
            with self._lock:
                tbl = self._open(collection_name)
                if tbl is None:
                    return
                tbl.delete(f"id = {_sql_str(str(memory_id))}")

        try:
            await asyncio.to_thread(_run)
        except Exception as e:
            raise QdrantError(f"Failed to delete memory from LanceDB: {e}") from e

    async def delete_context_points(
        self,
        workspace_id: str,
        context_id: str,
        collection_name: str = DEFAULT_COLLECTION,
    ) -> int:
        _validate_uuid(workspace_id, "workspace_id")
        _validate_uuid(context_id, "context_id")
        where = f"workspace_id = {_sql_str(workspace_id)} AND context_id = {_sql_str(context_id)}"

        def _run() -> int:
            with self._lock:
                tbl = self._open(collection_name)
                if tbl is None:
                    return 0
                count = tbl.count_rows(filter=where)
                tbl.delete(where)
                return int(count)

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            raise QdrantError(f"Failed to delete context points from LanceDB: {e}") from e
