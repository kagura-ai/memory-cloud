"""Qdrant vector database client.

Collection design (post Single Collection Migration, Issue #334):
- Default model (text-embedding-3-small, 512 dims) → `kagura_memories`
- Non-default models → `kagura_memories_{model_slug}_{dim}` via `get_collection_name()`
- Isolation is payload-based: every point carries `workspace_id` + `context_id`
- Named vectors: `dense` (VectorParams) + `bm25` (SparseVectorParams)
- Multilingual tokenizer (Qdrant 1.15+ built-in; Japanese auto-supported)
- Full-text index on summary + context_summary; keyword index on scope, type, context_id
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Condition,
    DatetimeRange,
    Disabled,
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    GeoLineString,
    GeoPoint,
    GeoPolygon,
    GeoRadius,
    IsEmptyCondition,
    MatchAny,
    MatchValue,
    Modifier,
    PayloadField,
    PointIdsList,
    PointStruct,
    QuantizationSearchParams,
    Range,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    SearchParams,
    SparseVector,
    SparseVectorParams,
    TextIndexParams,
    TokenizerType,
    VectorParams,
    VectorParamsDiff,
)

from config.database import QDRANT_URL
from utils.content_chunks import CHUNK_OF_FIELD, chunk_payload_updates
from utils.exceptions import QdrantError
from utils.geo_location import extract_near_filter, extract_within_filter
from utils.logger import get_logger
from utils.sparse_vector import build_query_sparse_vector
from utils.synonyms import expand_query_tokens
from utils.tag_normalize import group_tags_by_fold
from utils.tokenizer import augment_reading_tokens, tokenize_and_reading

if TYPE_CHECKING:
    from db.vector_store import VectorStore

logger = get_logger(__name__)

# Singleton Qdrant client
_qdrant_client: AsyncQdrantClient | None = None

# Single Collection Migration: Collection name constant
KAGURA_MEMORIES_COLLECTION = "kagura_memories"

# Named vector contract (Issue #16, #324): all kagura_memories collections use
# `{"dense": VectorParams(...), "bm25": SparseVectorParams(...)}` — anonymous
# vectors will fail with "Not existing vector name error".
KAGURA_MEMORIES_VECTOR_NAME = "dense"
KAGURA_MEMORIES_BM25_VECTOR_NAME = "bm25"

# ============================================================================
# Dense vector storage layout (#1990)
# ============================================================================

#: Scalar-quantization quantile: the 1% tails are clipped so one outlier does
#: not stretch the int8 range for every other vector (Qdrant's recommended value).
DENSE_QUANTIZATION_QUANTILE = 0.99

#: Candidates read from the int8 index per requested result before the fp32
#: rescore. 2.0 keeps the top-k order within Qdrant's documented <1% recall
#: loss for scalar quantization; a module constant until a deployment needs
#: to tune it.
DENSE_RESCORE_OVERSAMPLING = 2.0


@dataclass(frozen=True)
class DenseVectorConfig:
    """Operator-chosen layout of the ``dense`` named vector (#1990).

    ``quantization`` is ``"none"`` (fp32 index, the historical layout) or
    ``"int8"`` (scalar-quantized copy kept in RAM, searches rescored against
    the fp32 originals). ``on_disk`` stores the fp32 originals as mmap instead
    of RAM. Read from ``QDRANT_DENSE_QUANTIZATION`` / ``QDRANT_DENSE_ON_DISK``.
    """

    quantization: str = "none"
    on_disk: bool = False

    @classmethod
    def from_settings(cls) -> "DenseVectorConfig":
        """Build the config from the process settings."""
        from config.settings import get_settings

        settings = get_settings()
        return cls(
            quantization=settings.qdrant_dense_quantization,
            on_disk=bool(settings.qdrant_dense_on_disk),
        )

    @property
    def is_default(self) -> bool:
        """True when the config asks for the historical fp32-in-RAM layout."""
        return self.quantization == "none" and not self.on_disk


@dataclass(frozen=True)
class DenseVectorState:
    """What the server reports for a collection's ``dense`` vector.

    ``quantization`` is ``"none"``, ``"int8"`` (scalar int8 kept in RAM, the
    layout this service writes), ``"int8_on_disk"`` (scalar int8 with
    ``always_ram=False``, reconciled like a mismatch) or ``"other"`` (a
    product, binary or per-vector quantization set outside this service,
    which the reconciler leaves alone).
    """

    quantization: str
    on_disk: bool


def _dense_vector_params(embedding_dim: int, config: DenseVectorConfig) -> VectorParams:
    """VectorParams for a new collection's ``dense`` vector."""
    return VectorParams(
        size=embedding_dim,
        distance=Distance.COSINE,
        on_disk=True if config.on_disk else None,
    )


def _dense_quantization_config(config: DenseVectorConfig) -> ScalarQuantization | None:
    """Collection-level quantization for ``config``, None for fp32."""
    if config.quantization != "int8":
        return None
    return ScalarQuantization(
        scalar=ScalarQuantizationConfig(
            type=ScalarType.INT8,
            quantile=DENSE_QUANTIZATION_QUANTILE,
            always_ram=True,
        )
    )


def _dense_search_params() -> SearchParams:
    """Search params for the dense query: rescore int8 candidates with fp32.

    Harmless on an unquantized collection (the server ignores the block), so
    the search path does not need to know the collection's layout.
    """
    return SearchParams(
        quantization=QuantizationSearchParams(
            ignore=False,
            rescore=True,
            oversampling=DENSE_RESCORE_OVERSAMPLING,
        )
    )


def _read_dense_state(info: Any) -> DenseVectorState | None:
    """Parse ``get_collection()`` output; None when the layout is not ours.

    Only the named-vector layout (``{"dense": VectorParams}``) is reconciled.
    Quantization is compared by kind only: ``quantile`` is a float the server
    may normalise, and an equality test on it would re-send the same config
    at every startup and kick the optimizer each time.
    """
    config = getattr(info, "config", None)
    params = getattr(config, "params", None)
    vectors = getattr(params, "vectors", None)
    if not isinstance(vectors, dict) or KAGURA_MEMORIES_VECTOR_NAME not in vectors:
        return None
    dense = vectors[KAGURA_MEMORIES_VECTOR_NAME]
    quantization = getattr(config, "quantization_config", None)
    if getattr(dense, "quantization_config", None) is not None:
        # A per-vector setting overrides the collection-level one in Qdrant;
        # the service never writes one, so it was set by hand: leave it alone.
        kind = "other"
    elif quantization is None:
        kind = "none"
    else:
        scalar = getattr(quantization, "scalar", None)
        scalar_type = getattr(scalar, "type", None)
        if str(getattr(scalar_type, "value", scalar_type)) != "int8":
            kind = "other"
        elif getattr(scalar, "always_ram", None):
            kind = "int8"
        else:
            kind = "int8_on_disk"
    return DenseVectorState(quantization=kind, on_disk=bool(getattr(dense, "on_disk", None)))


async def _apply_dense_vector_config(
    client: AsyncQdrantClient,
    collection_name: str,
    config: DenseVectorConfig,
    *,
    info: Any | None = None,
) -> bool:
    """Reconcile an existing collection with ``config``; True when it was changed.

    Sends one ``update_collection`` carrying only the parts that differ
    (quantization and/or the dense vector's ``on_disk``), so a collection
    that already matches costs one ``get_collection`` and nothing else.
    Going back to ``"none"`` sends ``Disabled`` only when the server reports
    a quantization. The optimizer then rebuilds the segments in the
    background; searches keep using the old segments meanwhile.
    """
    if info is None:
        info = await client.get_collection(collection_name)
    state = _read_dense_state(info)
    if state is None:
        logger.warning(
            "collection_dense_config_skipped",
            collection=collection_name,
            reason="no named dense vector",
        )
        return False
    if state.quantization == "other":
        logger.warning(
            "collection_dense_config_skipped",
            collection=collection_name,
            reason="quantization configured outside this service",
        )
        return False

    changes: dict[str, Any] = {}
    if state.quantization != config.quantization:
        changes["quantization_config"] = _dense_quantization_config(config) or Disabled.DISABLED
    if state.on_disk != config.on_disk:
        changes["vectors_config"] = {
            KAGURA_MEMORIES_VECTOR_NAME: VectorParamsDiff(on_disk=config.on_disk),
        }
    if not changes:
        return False

    await client.update_collection(collection_name=collection_name, **changes)
    logger.info(
        "collection_dense_config_updated",
        collection=collection_name,
        changed=sorted(changes),
        quantization=config.quantization,
        on_disk=config.on_disk,
        previous_quantization=state.quantization,
        previous_on_disk=state.on_disk,
    )
    return True


async def apply_dense_vector_config(collection_name: str) -> bool:
    """Reconcile one collection with the configured dense layout (#1990).

    Returns True when ``update_collection`` was sent. Raises QdrantError when
    the server cannot be read or updated.
    """
    if _active_store() is not None:
        return False
    client = get_qdrant_client()
    try:
        return await _apply_dense_vector_config(
            client, collection_name, DenseVectorConfig.from_settings()
        )
    except Exception as e:
        raise QdrantError(f"Failed to apply dense vector config to '{collection_name}': {e}") from e


async def apply_dense_vector_config_to_all(
    collection_names: Sequence[str] | None = None,
) -> dict[str, bool]:
    """Startup pass (#1990): reconcile every ``kagura_memories*`` collection.

    Sequential on purpose — each change makes the optimizer rebuild that
    collection's segments, and the old and new segments coexist until the
    swap, so one collection at a time bounds the transient RAM and disk
    peak. A collection that fails is logged and skipped; the others still
    get their turn. Returns ``{collection: changed}``. Raises QdrantError
    when the collection list itself cannot be read.

    Args:
        collection_names: Restrict the pass to these collections (tests,
            tooling). Default: :func:`list_memory_collections`.

    Across API workers the pass is guarded by
    :func:`tasks.dense_vector_config.run_dense_vector_config_reconcile`
    (single-flight advisory lock), so a multi-worker deployment converts
    each collection once; call this directly only from one process.
    """
    config = DenseVectorConfig.from_settings()
    if _active_store() is not None:
        if not config.is_default:
            logger.warning(
                "dense_vector_config_ignored",
                backend="lance",
                quantization=config.quantization,
                on_disk=config.on_disk,
            )
        return {}

    client = get_qdrant_client()
    names = (
        list(collection_names) if collection_names is not None else await list_memory_collections()
    )
    results: dict[str, bool] = {}
    for name in names:
        try:
            results[name] = await _apply_dense_vector_config(client, name, config)
        except Exception as e:  # noqa: BLE001 — one collection must not stop the pass
            logger.warning("collection_dense_config_apply_failed", collection=name, error=str(e))
            results[name] = False
    return results


def get_collection_name(model: str, dimensions: int) -> str:
    """Get Qdrant collection name for a given embedding model.

    Default model (text-embedding-3-small, 512) maps to the legacy collection name
    for backward compatibility. Other models get a unique collection.

    Args:
        model: Embedding model name
        dimensions: Vector dimensions

    Returns:
        Collection name string
    """
    if model == "text-embedding-3-small" and dimensions == 512:
        return KAGURA_MEMORIES_COLLECTION
    slug = model.replace("-", "_").replace(":", "_").replace(".", "_").lower()
    return f"kagura_memories_{slug}_{dimensions}"


def _validate_uuid_format(value: str, field_name: str) -> None:
    """Validate UUID format to prevent injection attacks.

    Issue #273 H-4: Prevent filter injection via malformed UUIDs.

    Args:
        value: UUID string to validate
        field_name: Field name for error message

    Raises:
        ValueError: If UUID format is invalid

    Example:
        >>> _validate_uuid_format("123e4567-e89b-12d3-a456-426614174000", "workspace_id")  # OK
        >>> _validate_uuid_format("", "workspace_id")  # ValueError
        >>> _validate_uuid_format("invalid-uuid", "workspace_id")  # ValueError
    """
    from config.constants import ERROR_MSG_INVALID_UUID

    if not value or not isinstance(value, str):
        raise ValueError(ERROR_MSG_INVALID_UUID.format(field=field_name, value=value or "(empty)"))

    # Try to parse as UUID to validate format
    try:
        UUID(value)
    except (ValueError, AttributeError) as e:
        raise ValueError(ERROR_MSG_INVALID_UUID.format(field=field_name, value=value)) from e


# Bound on the tags a filter matches on: tags under "any" / exact "all", tag
# groups under "all" + tags_normalize (#2058).
_MAX_FILTER_TAGS = 50
# Bound on the spellings scanned to build those groups. expand_tag_filter adds
# at most MAX_EXPANSION_PER_TAG (20) spellings per tag, so 50 widened tags stay
# within it; the cap only limits the fold work on an oversized caller list.
_MAX_FILTER_SPELLINGS = 2000


def tag_groups_for_all(tags: list[str]) -> list[list[str]]:
    """The groups a tags_match='all' + tags_normalize filter ANDs (#2058).

    Groups BEFORE capping, so the variants of the first tags cannot push a
    later requested tag past the cap and silently drop it from the AND.

    Args:
        tags: The filter's non-empty string tags, uncapped.

    Returns:
        At most ``_MAX_FILTER_TAGS`` groups from ``group_tags_by_fold``.
    """
    return group_tags_by_fold(tags[:_MAX_FILTER_SPELLINGS])[:_MAX_FILTER_TAGS]


def _build_tag_filter_conditions(filters: dict[str, Any]) -> list[FieldCondition]:
    """Build FieldConditions for tag filtering.

    Issue #67: Exact-match tag filtering only. Tags are NOT added to
    BM25 text search to avoid score inflation for tag-heavy memories.
    Issue #79: Support AND logic via tags_match="all".
    Issue #2058: with tags_normalize, "all" requires each TAG, not each
    spelling: the spellings recall widened one tag to are one MatchAny.

    Args:
        filters: Filter dict with optional tags list, tags_match mode and
            tags_normalize flag

    Returns:
        List of FieldConditions for tag matching (empty if no tags)
    """
    filter_tags = filters.get("tags")
    if not isinstance(filter_tags, list) or not filter_tags:
        return []

    # Validate: only non-empty strings, bounded to 50 tags
    strings = [t for t in filter_tags if isinstance(t, str) and t]
    valid_tags = strings[:_MAX_FILTER_TAGS]
    if not valid_tags:
        return []

    tags_match = filters.get("tags_match", "any")
    if tags_match not in ("any", "all"):
        raise ValueError(f"Invalid tags_match value: {tags_match!r}. Must be 'any' or 'all'.")

    if tags_match == "all":
        # AND logic: memory must have ALL specified tags
        if not filters.get("tags_normalize"):
            return [FieldCondition(key="tags", match=MatchValue(value=tag)) for tag in valid_tags]
        # #2058: the list holds every stored spelling of each requested tag
        # (expand_tag_filter); any one spelling satisfies that tag. A tag with
        # one spelling keeps the exact MatchValue above.
        return [
            FieldCondition(key="tags", match=MatchValue(value=group[0]))
            if len(group) == 1
            else FieldCondition(key="tags", match=MatchAny(any=group))
            for group in tag_groups_for_all(strings)
        ]
    else:
        # OR logic (default): memory must have ANY of the specified tags
        return [FieldCondition(key="tags", match=MatchAny(any=valid_tags))]


def _build_date_filter_conditions(filters: dict[str, Any]) -> list[FieldCondition]:
    """Build FieldConditions for date range filtering (Issue #78).

    Args:
        filters: Filter dict with optional date range keys

    Returns:
        List of FieldConditions for date filtering (empty if no date filters)

    Raises:
        ValueError: If a date value is not a valid ISO 8601 string
    """
    from utils.datetime import parse_iso8601_to_aware

    date_filter_map = {
        "created_after": ("created_at", "gte"),
        "created_before": ("created_at", "lte"),
        "updated_after": ("updated_at", "gte"),
        "updated_before": ("updated_at", "lte"),
    }

    conditions: list[FieldCondition] = []

    for filter_key, (field, operator) in date_filter_map.items():
        value = filters.get(filter_key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"{filter_key} must be an ISO 8601 datetime string, got {type(value).__name__}"
            )
        dt = parse_iso8601_to_aware(value, filter_key)
        conditions.append(FieldCondition(key=field, range=DatetimeRange(**{operator: dt})))

    return conditions


def extract_score_threshold(filters: dict[str, Any] | None) -> float | None:
    """Extract and validate ``filters["score_threshold"]`` (#1229).

    A similarity floor for the SEMANTIC leg only — BM25 scores are not
    cosine similarities, so fulltext search ignores the key. ``filters`` is
    free-form user input on the public recall API, so a non-numeric or
    out-of-range value raises ValueError (→ 4xx, matching the
    importance-range convention) instead of surfacing as a 5xx downstream.
    """
    if not filters or "score_threshold" not in filters:
        return None
    value = filters["score_threshold"]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"Invalid score_threshold: {value!r} (expected a number in [-1, 1])")
    value = float(value)
    if not -1.0 <= value <= 1.0:
        raise ValueError(f"Invalid score_threshold: {value!r} (expected a number in [-1, 1])")
    return value


def _build_search_filter(
    workspace_id: str,
    context_id: str | list[str],
    user_id: str,
    is_shared_context: bool = False,
    filters: dict[str, Any] | None = None,
    owner_ids: Sequence[str] | None = None,
) -> Filter | None:
    """Build combined isolation + metadata filter for search queries.

    Shared by semantic search and BM25 search to avoid duplication.

    Args:
        workspace_id: Workspace ID (isolation)
        context_id: Single context ID or list of context IDs (Issue #81: cross-context recall)
        user_id: User ID (isolation, skipped for shared contexts)
        is_shared_context: If True, skip user_id filter
        filters: Optional metadata filters (scope, type, importance, tags, date ranges)
        owner_ids: ``user_id`` plus the accounts linked to it (#1784). When
            given, the private filter matches any of them instead of
            ``user_id`` alone.

    Returns:
        Qdrant Filter or None
    """
    conditions: list[Condition] = [
        FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
    ]

    # Issue #81: Support single or multiple context IDs
    if isinstance(context_id, list):
        conditions.append(FieldCondition(key="context_id", match=MatchAny(any=context_id)))
    else:
        conditions.append(FieldCondition(key="context_id", match=MatchValue(value=context_id)))

    if not is_shared_context:
        if owner_ids and set(owner_ids) != {user_id}:
            conditions.append(
                FieldCondition(key="user_id", match=MatchAny(any=sorted({user_id, *owner_ids})))
            )
        else:
            conditions.append(FieldCondition(key="user_id", match=MatchValue(value=user_id)))

    if filters:
        if "scope" in filters:
            conditions.append(FieldCondition(key="scope", match=MatchValue(value=filters["scope"])))
        if "type" in filters:
            conditions.append(FieldCondition(key="type", match=MatchValue(value=filters["type"])))
        importance_condition = _build_importance_range_condition(filters)
        if importance_condition:
            conditions.append(importance_condition)
        tag_conditions = _build_tag_filter_conditions(filters)
        conditions.extend(tag_conditions)
        date_conditions = _build_date_filter_conditions(filters)
        conditions.extend(date_conditions)
        # WHERE axis (#1332): filters["near"] → geo_radius over the
        # ``location`` payload (written from the location_lat/lon generated
        # columns). Points without the field — no location, resource chunks —
        # are excluded by must semantics. extract_near_filter fails closed on
        # malformed input (#1229 lesson; LocationValidationError is a
        # ValueError, mapping to the same 4xx path as the importance range).
        near = extract_near_filter(filters)
        if near is not None:
            near_lat, near_lon, near_radius_m = near
            conditions.append(
                FieldCondition(
                    key="location",
                    geo_radius=GeoRadius(
                        center=GeoPoint(lat=near_lat, lon=near_lon),
                        radius=near_radius_m,
                    ),
                )
            )
        # WHERE axis (#1335): filters["within"] → geo_polygon geofence over
        # the same payload field. NOTE: Qdrant's geo index accelerates only
        # bbox/radius — the polygon condition filters by scan, hence the
        # vertex cap in extract_within_filter. Composes with near (AND).
        within = extract_within_filter(filters)
        if within is not None:
            conditions.append(
                FieldCondition(
                    key="location",
                    geo_polygon=GeoPolygon(
                        exterior=GeoLineString(
                            points=[GeoPoint(lat=v_lat, lon=v_lon) for v_lat, v_lon in within]
                        )
                    ),
                )
            )

    return Filter(must=conditions) if conditions else None


def _build_importance_range_condition(filters: dict[str, Any]) -> FieldCondition | None:
    """Build FieldCondition for importance range filtering.

    Issue #139: Helper function to avoid code duplication.

    Args:
        filters: Filter dict with optional importance range

    Returns:
        FieldCondition for importance range or None

    Raises:
        ValueError: If range values are invalid (gte > lte)

    Example:
        >>> filters = {"importance": {"gte": 0.9, "lte": 0.95}}
        >>> condition = _build_importance_range_condition(filters)
    """
    if "importance" not in filters or not isinstance(filters["importance"], dict):
        return None

    importance_filter = filters["importance"]
    range_kwargs = {}

    for op in ["gte", "lte", "gt", "lt"]:
        if op in importance_filter:
            range_kwargs[op] = importance_filter[op]

    if not range_kwargs:
        return None

    # Validate range consistency
    if "gte" in range_kwargs and "lte" in range_kwargs:
        if range_kwargs["gte"] > range_kwargs["lte"]:
            raise ValueError(
                f"gte ({range_kwargs['gte']}) cannot be greater than lte ({range_kwargs['lte']})"
            )

    if "gt" in range_kwargs and "lt" in range_kwargs:
        if range_kwargs["gt"] >= range_kwargs["lt"]:
            raise ValueError(
                f"gt ({range_kwargs['gt']}) must be less than lt ({range_kwargs['lt']})"
            )

    return FieldCondition(key="importance", range=Range(**range_kwargs))


def get_qdrant_client() -> AsyncQdrantClient:
    """Get singleton Qdrant client.

    Issue #273 Security: Supports API key authentication for production.

    Returns:
        AsyncQdrantClient instance

    Raises:
        QdrantError: If connection fails
    """
    global _qdrant_client

    if _qdrant_client is None:
        try:
            from config.settings import get_settings

            settings = get_settings()

            # Issue #273 Security Review: Add API key auth for production
            if settings.qdrant_api_key:
                _qdrant_client = AsyncQdrantClient(url=QDRANT_URL, api_key=settings.qdrant_api_key)
                logger.info("qdrant_client_initialized", url=QDRANT_URL, authenticated=True)
            else:
                _qdrant_client = AsyncQdrantClient(url=QDRANT_URL)
                logger.info("qdrant_client_initialized", url=QDRANT_URL, authenticated=False)
        except Exception as e:
            raise QdrantError(f"Failed to connect to Qdrant: {e}") from e

    return _qdrant_client


def _active_store() -> "VectorStore | None":
    """Return the alternative VectorStore backend, or None for the Qdrant path.

    "Kagura Lite" seam: when ``vector_backend == "qdrant"`` (default) this
    returns ``None`` and the functions below run their native Qdrant code
    unchanged. When set to ``"lance"`` they delegate to the in-process
    LanceVectorStore. The import is lazy to avoid a circular import.
    """
    from db.vector_store import get_active_store

    return get_active_store()


# ============================================================================
# Single Collection Design (Issue #273)
# ============================================================================
# All memories stored in single collection: kagura_memories
# Isolation via payload filtering: workspace_id + context_id + user_id
# See migrations/062_*.sql and 063_*.sql for 3-level isolation implementation
# ============================================================================


async def add_memory_to_qdrant(
    user_id: str,
    memory_id: UUID,
    vector: list[float],
    payload: dict[str, Any],
    workspace_id: str,
    context_id: str,
    sparse_indices: list[int] | None = None,
    sparse_values: list[float] | None = None,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Add memory point to Qdrant with dense + sparse vectors and 3-level isolation.

    Issue #16: Supports named vectors (dense for semantic, bm25 for sparse BM25).

    Args:
        user_id: User ID (required)
        memory_id: Memory UUID
        vector: Dense embedding vector
        payload: Metadata payload
        workspace_id: Workspace ID (required for 3-level isolation)
        context_id: Context ID (required for 3-level isolation)
        sparse_indices: Sparse vector token indices (MurmurHash3)
        sparse_values: Sparse vector TF values

    Raises:
        QdrantError: If operation fails
        ValueError: If workspace_id or context_id is missing
    """
    # Validate the sparse pair up-front so every backend (Qdrant + Lance)
    # rejects a mismatched pair identically (Lance ignores sparse vectors).
    if (sparse_indices is None) ^ (sparse_values is None):
        raise ValueError("sparse_indices and sparse_values must be provided together or both None")
    if (
        sparse_indices is not None
        and sparse_values is not None
        and len(sparse_indices) != len(sparse_values)
    ):
        raise ValueError("sparse_indices and sparse_values must have the same length")

    _store = _active_store()
    if _store is not None:
        return await _store.add_memory(
            user_id,
            memory_id,
            vector,
            payload,
            workspace_id,
            context_id,
            sparse_indices,
            sparse_values,
            collection_name,
        )

    client = get_qdrant_client()

    # Validate required parameters
    if not workspace_id or not context_id or not user_id:
        raise ValueError(
            "workspace_id, context_id, and user_id are required. "
            f"Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
        )

    # Issue #273 H-4: Validate UUID format to prevent filter injection
    _validate_uuid_format(workspace_id, "workspace_id")
    _validate_uuid_format(context_id, "context_id")

    # Add isolation fields to payload
    payload["workspace_id"] = workspace_id
    payload["context_id"] = context_id
    payload["user_id"] = user_id

    try:
        # Issue #16: Named vectors (dense + sparse BM25)
        point_vector: dict[str, Any] = {KAGURA_MEMORIES_VECTOR_NAME: vector}
        if sparse_indices and sparse_values:
            point_vector[KAGURA_MEMORIES_BM25_VECTOR_NAME] = SparseVector(
                indices=sparse_indices, values=sparse_values
            )

        await client.upsert(
            collection_name=collection_name,
            points=[
                PointStruct(
                    id=str(memory_id),
                    vector=point_vector,
                    payload=payload,
                )
            ],
        )

        logger.debug(
            "memory_added_to_qdrant",
            collection=collection_name,
            memory_id=str(memory_id),
            workspace_id=workspace_id,
            context_id=context_id,
        )

    except Exception as e:
        raise QdrantError(f"Failed to add memory: {e}") from e


async def search_memories_qdrant(
    user_id: str,
    query_vector: list[float],
    workspace_id: str,
    context_id: str | list[str],
    limit: int = 10,
    filters: dict[str, Any] | None = None,
    is_shared_context: bool = False,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
    include_vectors: bool = False,
    owner_ids: Sequence[str] | None = None,
    include_chunks: bool | Sequence[str] = False,
) -> list[dict]:
    """Semantic search in Qdrant with workspace-aware isolation.

    Single Collection Migration: Always uses "kagura_memories" collection.
    Issue #81: context_id can be a single string or list for cross-context recall.

    Args:
        user_id: User ID (required for isolation)
        query_vector: Query embedding vector
        workspace_id: Workspace ID (required for isolation)
        context_id: Context ID or list of context IDs (Issue #81)
        limit: Max results
        filters: Optional filters (scope, type, importance range, etc.)
        is_shared_context: If True, skip user_id filter (workspace members can access)
        include_chunks: #2015 — also return content chunk points (``chunk_of``
            set). Off by default, so every caller that assumes one point per
            memory sees summary points only; recall opts in and collapses.
            Ignored by non-Qdrant stores, which never hold chunk points.
            A sequence of context ids returns chunk points of those
            contexts only (cross-context recall: the contexts that opted
            in); an empty sequence is the same as False.

    Returns:
        List of scored results

    Raises:
        QdrantError: If search fails
        ValueError: If workspace_id, context_id, or user_id is missing
    """
    _store = _active_store()
    if _store is not None:
        return await _store.search_semantic(
            user_id,
            query_vector,
            workspace_id,
            context_id,
            limit,
            filters,
            is_shared_context,
            collection_name,
            include_vectors,
            owner_ids=owner_ids,
        )

    client = get_qdrant_client()

    # CRITICAL: Validate isolation parameters (Security)
    if not workspace_id or not context_id or not user_id:
        raise ValueError(
            "Isolation requires workspace_id, context_id, and user_id. "
            f"Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
        )

    # Issue #273 H-4: Validate UUID format to prevent filter injection
    _validate_uuid_format(workspace_id, "workspace_id")
    if isinstance(context_id, list):
        for cid in context_id:
            _validate_uuid_format(cid, "context_id")
    else:
        _validate_uuid_format(context_id, "context_id")

    # Build filter outside try/except so ValueError propagates as 4xx, not QdrantError
    qdrant_filter = _build_search_filter(
        workspace_id, context_id, user_id, is_shared_context, filters, owner_ids
    )
    if not include_chunks:
        # #2015: summary (and resource) points only — chunk points carry chunk_of.
        chunk_exclusion = summary_points_condition()
        if qdrant_filter is None:
            qdrant_filter = Filter(must=[chunk_exclusion])
        else:
            qdrant_filter.must = [*(qdrant_filter.must or []), chunk_exclusion]  # type: ignore[misc]
    elif not isinstance(include_chunks, bool):
        # #2015 PR 2a: per-context chunk points — a point is a summary point,
        # or a chunk of one of the listed (opted-in) contexts. A leftover
        # chunk of a context whose flag is off never matches.
        chunk_contexts = (
            [include_chunks]
            if isinstance(include_chunks, str)
            else [str(cid) for cid in include_chunks]
        )
        for cid in chunk_contexts:
            _validate_uuid_format(cid, "include_chunks")
        per_context_chunks = Filter(
            should=[
                summary_points_condition(),
                FieldCondition(key="context_id", match=MatchAny(any=chunk_contexts)),
            ]
        )
        if qdrant_filter is None:
            qdrant_filter = Filter(must=[per_context_chunks])
        else:
            qdrant_filter.must = [*(qdrant_filter.must or []), per_context_chunks]  # type: ignore[misc]

    # #1229: score_threshold is a query_points kwarg, not a payload condition —
    # _build_search_filter used to drop it silently, so dedup/edge-discovery
    # candidate thresholds never applied (every top-k neighbor qualified).
    # Validated outside the try so junk user input maps to 4xx, not 5xx.
    score_threshold = extract_score_threshold(filters)

    try:
        # Issue #16: Named vector for semantic search
        results = await client.query_points(
            collection_name=collection_name,
            query=query_vector,
            using=KAGURA_MEMORIES_VECTOR_NAME,
            limit=limit,
            query_filter=qdrant_filter,
            score_threshold=score_threshold,
            with_vectors=[KAGURA_MEMORIES_VECTOR_NAME] if include_vectors else False,
            # #1990: rescore int8 candidates against the fp32 originals;
            # ignored by the server when the collection is not quantized.
            search_params=_dense_search_params(),
        )

        return [
            {
                "id": point.id,
                "score": point.score,
                "payload": point.payload,
                "embedding": (
                    point.vector.get(KAGURA_MEMORIES_VECTOR_NAME, [])
                    if include_vectors and isinstance(point.vector, dict)
                    else []
                ),
            }
            for point in results.points
        ]

    except Exception as e:
        raise QdrantError(f"Search failed: {e}") from e


async def update_memory_payload_in_qdrant(
    memory_id: UUID,
    payload_updates: dict[str, Any],
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
    delete_keys: list[str] | None = None,
    *,
    cascade_chunks: bool = True,
) -> None:
    """Update payload fields of a Qdrant point without re-embedding.

    Uses set_payload to update only specified fields (e.g. tags, importance,
    type) and delete_payload to remove keys whose backing data was removed
    (#1332: a details write that drops ``location`` must clear the geo
    payload, not leave a stale coordinate matching ``filters.near``).

    Args:
        memory_id: Memory UUID
        payload_updates: Dict of payload fields to update
        collection_name: Qdrant collection name
        delete_keys: Payload keys to remove from the point
        cascade_chunks: #2015 — also apply the change to the memory's content
            chunk points. A caller that knows the context has
            ``content_chunks_enabled`` off may pass False: such a context has
            no chunk points. Default True (always correct, one or two extra
            calls).

    Raises:
        QdrantError: If operation fails
    """
    _store = _active_store()
    if _store is not None:
        return await _store.update_payload(
            memory_id, payload_updates, collection_name, delete_keys=delete_keys
        )

    client = get_qdrant_client()

    try:
        if payload_updates:
            await client.set_payload(
                collection_name=collection_name,
                payload=payload_updates,
                points=[str(memory_id)],
            )
        if delete_keys:
            await client.delete_payload(
                collection_name=collection_name,
                keys=delete_keys,
                points=[str(memory_id)],
            )

        if cascade_chunks:
            await _cascade_payload_to_chunks(
                client, memory_id, payload_updates, delete_keys, collection_name
            )

        logger.debug(
            "memory_payload_updated_in_qdrant",
            collection=collection_name,
            memory_id=str(memory_id),
            updated_fields=list(payload_updates.keys()),
            deleted_fields=delete_keys or [],
        )

    except Exception as e:
        raise QdrantError(f"Failed to update memory payload: {e}") from e


def summary_points_condition() -> IsEmptyCondition:
    """The condition that keeps summary (and resource) points: ``chunk_of`` unset.

    Content chunk points (#2015) carry ``chunk_of``; everything that assumes
    one point per memory filters on this (#2041).
    """
    return IsEmptyCondition(is_empty=PayloadField(key=CHUNK_OF_FIELD))


def summary_points_filter(*must: Condition, must_not: Sequence[Condition] | None = None) -> Filter:
    """A filter of ``must`` (and ``must_not``) that also leaves chunk points out (#2041).

    The one way to build the ``scroll_filter`` of a summary-only scroll:
    ``tests/test_summary_points_scroll_guard.py`` fails on a ``.scroll(`` call
    that does not use it, unless the function is allow-listed there.
    """
    return Filter(
        must=[*must, summary_points_condition()],
        must_not=list(must_not) if must_not is not None else None,
    )


def _chunks_of_filter(memory_ids: Sequence[UUID | str]) -> Filter:
    """Filter selecting the content chunk points (#2015) of ``memory_ids``."""
    return Filter(
        must=[
            FieldCondition(key=CHUNK_OF_FIELD, match=MatchAny(any=[str(mid) for mid in memory_ids]))
        ]
    )


async def _cascade_payload_to_chunks(
    client: AsyncQdrantClient,
    memory_id: UUID,
    payload_updates: dict[str, Any],
    delete_keys: list[str] | None,
    collection_name: str,
) -> None:
    """Apply a summary-point payload change to the memory's chunk points (#2015).

    Chunk points copy the filterable fields of their summary point, so a tag /
    importance / type / location change must reach them too or a filtered
    recall would match a chunk on stale metadata — and an isolation field
    (``user_id``, moved by the creator transfer) must never stay behind. BM25
    token fields are not copied (chunk points have no sparse vector).

    On failure the memory's chunk points are deleted instead (its next
    re-embed rebuilds them), so no chunk keeps stale metadata. Only when that
    delete fails too is :class:`QdrantError` raised.
    """
    chunk_updates = chunk_payload_updates(payload_updates)
    if not chunk_updates and not delete_keys:
        return
    chunk_filter = Filter(
        must=[FieldCondition(key=CHUNK_OF_FIELD, match=MatchValue(value=str(memory_id)))]
    )
    try:
        if chunk_updates:
            await client.set_payload(
                collection_name=collection_name, payload=chunk_updates, points=chunk_filter
            )
        if delete_keys:
            await client.delete_payload(
                collection_name=collection_name, keys=delete_keys, points=chunk_filter
            )
    except Exception as e:
        logger.warning(
            "content_chunks_payload_cascade_failed",
            collection=collection_name,
            memory_id=str(memory_id),
            error_class=type(e).__name__,
        )
        try:
            await client.delete(
                collection_name=collection_name,
                points_selector=FilterSelector(filter=chunk_filter),
            )
        except Exception as delete_error:
            raise QdrantError(
                f"Failed to update or remove content chunks of {memory_id}: {delete_error}"
            ) from delete_error


def chunk_points_supported() -> bool:
    """Whether the active vector store holds content chunk points (#2015).

    Qdrant only: the LanceDB store keeps one vector per memory.
    """
    return _active_store() is None


async def upsert_chunk_points(
    points: Sequence[tuple[str, list[float], dict[str, Any]]],
    *,
    user_id: str,
    workspace_id: str,
    context_id: str,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Upsert a memory's content chunk points (#2015) in one call.

    Each item is ``(point_id, dense_vector, payload)`` with the payload from
    ``utils.content_chunks.build_chunk_payload``. Dense vector only — chunk
    points have no ``bm25`` sparse vector. The isolation fields are added here,
    as ``add_memory_to_qdrant`` does for a summary point. A no-op on a
    non-Qdrant store (chunk vectors are Qdrant-only).
    """
    if not points or _active_store() is not None:
        return
    if not workspace_id or not context_id or not user_id:
        raise ValueError("workspace_id, context_id, and user_id are required")
    _validate_uuid_format(workspace_id, "workspace_id")
    _validate_uuid_format(context_id, "context_id")

    client = get_qdrant_client()
    structs = [
        PointStruct(
            id=point_id,
            vector={KAGURA_MEMORIES_VECTOR_NAME: vector},
            payload={
                **payload,
                "workspace_id": workspace_id,
                "context_id": context_id,
                "user_id": user_id,
            },
        )
        for point_id, vector, payload in points
    ]
    try:
        await client.upsert(collection_name=collection_name, points=structs)
    except Exception as e:
        raise QdrantError(f"Failed to add content chunks: {e}") from e


async def delete_chunk_points(
    memory_ids: Sequence[UUID | str],
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Delete the content chunk points (#2015) of ``memory_ids``.

    Filter-based, so it also removes stale chunks past the current chunk
    count. A no-op for an empty list or on a non-Qdrant store.
    """
    if not memory_ids or _active_store() is not None:
        return
    client = get_qdrant_client()
    try:
        await client.delete(
            collection_name=collection_name,
            points_selector=FilterSelector(filter=_chunks_of_filter(memory_ids)),
        )
    except Exception as e:
        raise QdrantError(f"Failed to delete content chunks: {e}") from e


async def purge_context_chunk_points(
    workspace_id: str,
    context_id: str,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Delete every content chunk point (#2015) of one context.

    Run when a context turns ``content_chunks_enabled`` off, so the invariant
    "flag off ⇒ no chunk points" holds. Summary and resource points are kept
    (``chunk_of`` must be set). A no-op on a non-Qdrant store.

    Raises:
        QdrantError: If the delete fails.
    """
    if _active_store() is not None:
        return
    _validate_uuid_format(workspace_id, "workspace_id")
    _validate_uuid_format(context_id, "context_id")
    client = get_qdrant_client()
    try:
        await client.delete(
            collection_name=collection_name,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                        FieldCondition(key="context_id", match=MatchValue(value=context_id)),
                    ],
                    must_not=[summary_points_condition()],
                )
            ),
        )
    except Exception as e:
        raise QdrantError(f"Failed to purge content chunks: {e}") from e


async def purge_context_chunk_points_everywhere(
    workspace_id: str,
    context_id: str,
    *,
    routed_collection: str,
    collections: list[str] | None = None,
) -> list[str]:
    """Delete a context's content chunk points (#2015) from every ``kagura_memories*`` collection.

    A context routes to one collection, but an embedding migration keeps its
    points in two (the source until it is purged, the target from the
    re-embed on), so "flag off ⇒ no chunk points" has to hold in every
    collection, not only the routed one. One filter delete per collection,
    the routed one first.

    Only the routed collection is required: recall reads chunk points from
    it alone, so its failure is raised (the caller keeps the flag as it
    was). A failure in any other collection is logged and returned, so an
    unrelated broken collection never keeps an owner from turning the flag
    off; the embedding migration's verify removes a context's chunk points
    from its target again while the flag is off. A no-op on a non-Qdrant
    store.

    Args:
        routed_collection: The collection the context routes to.
        collections: The other collections to purge, next to the routed one
            (default: list them now — a routed collection the listing does
            not show does not exist yet and is skipped; a failed listing
            purges the routed collection only, and is logged).

    Returns:
        The other collections whose purge failed.

    Raises:
        QdrantError: If the routed collection's purge fails.
    """
    if _active_store() is not None:
        return []
    if collections is None:
        try:
            listed = await list_memory_collections()
        except QdrantError as e:
            logger.warning(
                "content_chunks_purge_list_failed",
                context_id=context_id,
                error_class=type(e).__name__,
            )
            names = [routed_collection]
        else:
            # A routed collection that does not exist yet (no memory was ever
            # written under that model) holds nothing to purge.
            names = [
                *([routed_collection] if routed_collection in listed else []),
                *(c for c in listed if c != routed_collection),
            ]
    else:
        names = [routed_collection, *(c for c in collections if c != routed_collection)]
    failed: list[str] = []
    for collection_name in names:
        try:
            await purge_context_chunk_points(workspace_id, context_id, collection_name)
        except QdrantError as e:
            if collection_name == routed_collection:
                raise
            failed.append(collection_name)
            logger.warning(
                "content_chunks_purge_failed",
                context_id=context_id,
                collection=collection_name,
                error_class=type(e).__name__,
            )
    return failed


async def count_context_chunk_points(
    workspace_id: str,
    context_id: str,
    collection_name: str,
    *,
    memory_ids: Sequence[UUID | str] | None = None,
    page_size: int = 1000,
) -> dict[str, int]:
    """A context's content chunk points (#2015) in ``collection_name``, per memory.

    Returns ``{chunk_of memory id: number of chunk points}``. Scrolls the
    chunk points only (``chunk_of`` set), with no payload but ``chunk_of``
    and no vectors; with ``memory_ids``, only the chunks of those memories. The embedding migration's verify uses it to report
    memories whose chunks are incomplete and to find chunks of memories that
    are no longer live. A non-Qdrant store holds none (``{}``).

    Raises:
        QdrantError: If the scroll fails.
    """
    if _active_store() is not None:
        return {}
    _validate_uuid_format(workspace_id, "workspace_id")
    _validate_uuid_format(context_id, "context_id")
    must: list[Condition] = [
        FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
        FieldCondition(key="context_id", match=MatchValue(value=context_id)),
    ]
    if memory_ids is not None:
        if not memory_ids:
            return {}
        must.append(
            FieldCondition(key=CHUNK_OF_FIELD, match=MatchAny(any=[str(m) for m in memory_ids]))
        )
    client = get_qdrant_client()
    chunk_filter = Filter(must=must, must_not=[summary_points_condition()])
    counts: dict[str, int] = {}
    offset = None
    try:
        while True:
            points, offset = await client.scroll(
                collection_name=collection_name,
                scroll_filter=chunk_filter,
                limit=page_size,
                offset=offset,
                with_payload=[CHUNK_OF_FIELD],
                with_vectors=False,
            )
            for point in points:
                chunk_of = (point.payload or {}).get(CHUNK_OF_FIELD)
                if chunk_of:
                    counts[str(chunk_of)] = counts.get(str(chunk_of), 0) + 1
            if offset is None or not points:
                return counts
    except Exception as e:
        raise QdrantError(f"Failed to count content chunks: {e}") from e


async def delete_memory_from_qdrant(
    user_id: str,
    memory_id: UUID,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Delete memory point from Qdrant (single collection migration).

    Single Collection Migration: Always uses "kagura_memories" collection.
    Since memory_id is globally unique, direct deletion by ID is safe.

    Args:
        user_id: User ID (for logging only)
        memory_id: Memory UUID (globally unique identifier)

    Raises:
        QdrantError: If deletion fails

    Note:
        Single Collection Migration: Always uses "kagura_memories" collection.
        Memory ID is globally unique, so direct deletion by ID is safe.
    """
    _store = _active_store()
    if _store is not None:
        return await _store.delete_memory(user_id, memory_id, collection_name)

    client = get_qdrant_client()

    try:
        await client.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=[str(memory_id)]),
        )

        logger.debug(
            "memory_deleted_from_qdrant",
            collection=collection_name,
            memory_id=str(memory_id),
            user_id=user_id,
        )

    except Exception as e:
        raise QdrantError(f"Failed to delete memory: {e}") from e

    # #2015: the memory's chunk points go with it. Fail-soft — the summary
    # point is gone, and the orphan sweep removes chunks whose memory row is
    # missing or tombstoned.
    try:
        await delete_chunk_points([memory_id], collection_name)
    except QdrantError as e:
        logger.warning(
            "content_chunks_delete_failed",
            collection=collection_name,
            memory_id=str(memory_id),
            error_class=type(e.__cause__ or e).__name__,
        )


async def search_memories_fulltext(
    user_id: str,
    query: str,
    workspace_id: str,
    context_id: str | list[str],
    limit: int = 10,
    filters: dict[str, Any] | None = None,
    is_shared_context: bool = False,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
    owner_ids: Sequence[str] | None = None,
) -> list[dict]:
    """BM25 keyword search using Qdrant native sparse vectors.

    Issue #16: Replaces MatchText + manual TF scoring with native BM25.
    Issue #81: context_id can be a single string or list for cross-context recall.

    Args:
        user_id: User ID (required for isolation)
        query: Search query (natural language)
        workspace_id: Workspace ID (required for isolation)
        context_id: Context ID or list of context IDs (Issue #81)
        limit: Max results
        filters: Optional filters (scope, type, importance, tags)
        is_shared_context: If True, skip user_id filter

    Returns:
        List of scored results with native BM25 scores

    Raises:
        QdrantError: If search fails
        ValueError: If isolation parameters are missing
    """
    _store = _active_store()
    if _store is not None:
        return await _store.search_fulltext(
            user_id,
            query,
            workspace_id,
            context_id,
            limit,
            filters,
            is_shared_context,
            collection_name,
            owner_ids=owner_ids,
        )

    client = get_qdrant_client()

    # CRITICAL: Validate isolation parameters (Security)
    if not workspace_id or not context_id or not user_id:
        raise ValueError(
            "Isolation requires workspace_id, context_id, and user_id. "
            f"Got workspace_id={workspace_id}, context_id={context_id}, user_id={user_id}"
        )

    _validate_uuid_format(workspace_id, "workspace_id")
    if isinstance(context_id, list):
        for cid in context_id:
            _validate_uuid_format(cid, "context_id")
    else:
        _validate_uuid_format(context_id, "context_id")

    # Build filter outside try/except so ValueError propagates as 4xx, not QdrantError
    qdrant_filter = _build_search_filter(
        workspace_id, context_id, user_id, is_shared_context, filters, owner_ids
    )

    try:
        # Build sparse query vector: single Sudachi pass for lemmas + readings
        tokenized_query, query_reading, sudachi_tokens = tokenize_and_reading(query)
        combined_query = f"{tokenized_query} {query_reading}" if query_reading else tokenized_query

        augmented = augment_reading_tokens(query, sudachi_tokens=sudachi_tokens)
        if augmented:
            combined_query = f"{combined_query} {augmented}"

        expanded_query = expand_query_tokens(combined_query)
        query_indices, query_values = build_query_sparse_vector(expanded_query)

        if not query_indices:
            logger.debug("bm25_query_empty_after_tokenization", query=query[:50])
            return []

        # Native BM25 search via sparse vector (Qdrant handles IDF + length norm)
        results = await client.query_points(
            collection_name=collection_name,
            query=SparseVector(indices=query_indices, values=query_values),
            using=KAGURA_MEMORIES_BM25_VECTOR_NAME,
            limit=limit,
            query_filter=qdrant_filter,
        )

        return [
            {
                "id": point.id,
                "score": point.score,
                "payload": point.payload,
            }
            for point in results.points
        ]

    except Exception as e:
        raise QdrantError(f"BM25 search failed: {e}") from e


# ============================================================================
# Single Collection Migration (Issue: Qdrant Single Collection)
# ============================================================================


async def ensure_kagura_memories_collection(
    embedding_dim: int = 512,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> None:
    """Ensure single kagura_memories collection exists with 3-level isolation indexes.

    Creates a single unified collection for all workspaces, contexts, and users.
    Isolation is achieved through payload filtering on workspace_id, context_id, user_id.

    This replaces the per-context collection design (kagura_workspace_{workspace_id}_context_{name})
    with a single collection + payload filtering approach for better scalability.

    Indexes created:
        - workspace_id (keyword) - CRITICAL for workspace-level isolation
        - context_id (keyword) - CRITICAL for context-level isolation
        - user_id (keyword) - CRITICAL for user-level isolation
        - summary (text, multilingual)
        - context_summary (text, multilingual)
        - scope (keyword)
        - type (keyword)
        - importance (float)

    Args:
        embedding_dim: Embedding dimension (default: 512 for text-embedding-3-small)

    Raises:
        QdrantError: If collection creation fails

    Example:
        >>> await ensure_kagura_memories_collection(512)
        # Creates "kagura_memories" collection with all indexes
    """
    _store = _active_store()
    if _store is not None:
        return await _store.ensure_collection(embedding_dim, collection_name)

    client = get_qdrant_client()

    try:
        # Check if collection exists
        collections = await client.get_collections()
        exists = any(c.name == collection_name for c in collections.collections)

        if exists:
            # Issue #16: Check if collection has sparse vector config
            info = await client.get_collection(collection_name)
            params = getattr(info.config, "params", None) if info.config else None
            sparse_cfg = getattr(params, "sparse_vectors", None) if params else None
            has_sparse = sparse_cfg is not None and KAGURA_MEMORIES_BM25_VECTOR_NAME in sparse_cfg

            if has_sparse:
                # The dense layout (#1990) of an existing collection is NOT
                # reconciled here: this runs on the request path in every
                # worker, and update_collection must be sent once per
                # deployment — the startup pass owns that.
                # Collection is up-to-date, ensure late-added indexes
                existing_fields = set(info.payload_schema.keys()) if info.payload_schema else set()
                if "tags" not in existing_fields:
                    await client.create_payload_index(
                        collection_name=collection_name,
                        field_name="tags",
                        field_schema="keyword",  # type: ignore[arg-type]
                    )
                    logger.info("created_missing_index", field="tags", type="keyword")
                if "location" not in existing_fields:
                    # WHERE axis (#1332): geo index retrofit for collections
                    # created before the location payload existed.
                    await client.create_payload_index(
                        collection_name=collection_name,
                        field_name="location",
                        field_schema="geo",  # type: ignore[arg-type]
                    )
                    logger.info("created_missing_index", field="location", type="geo")
                if CHUNK_OF_FIELD not in existing_fields:
                    # #2015: content chunk points are selected by chunk_of
                    # (exclusion in search, cascade deletes / payload updates).
                    await client.create_payload_index(
                        collection_name=collection_name,
                        field_name=CHUNK_OF_FIELD,
                        field_schema="keyword",  # type: ignore[arg-type]
                    )
                    logger.info("created_missing_index", field=CHUNK_OF_FIELD, type="keyword")
                return

            # Old collection without sparse vectors — requires manual migration
            import os

            if os.getenv("KAGURA_RECREATE_COLLECTIONS", "").lower() not in ("true", "1"):
                raise QdrantError(
                    f"Collection '{collection_name}' needs sparse vector migration. "
                    "Set KAGURA_RECREATE_COLLECTIONS=true to auto-recreate (destroys data)."
                )

            logger.warning(
                "collection_recreating",
                collection=collection_name,
                reason="missing sparse vector config",
            )
            await client.delete_collection(collection_name)

        # Create collection with dense + sparse vector config (Issue #16);
        # the dense layout (quantization, on_disk) follows the settings (#1990).
        dense_config = DenseVectorConfig.from_settings()
        await client.create_collection(
            collection_name=collection_name,
            vectors_config={
                KAGURA_MEMORIES_VECTOR_NAME: _dense_vector_params(embedding_dim, dense_config),
            },
            sparse_vectors_config={
                KAGURA_MEMORIES_BM25_VECTOR_NAME: SparseVectorParams(
                    modifier=Modifier.IDF,
                ),
            },
            quantization_config=_dense_quantization_config(dense_config),
        )

        logger.info(
            "single_collection_created",
            collection=collection_name,
            dim=embedding_dim,
            quantization=dense_config.quantization,
            on_disk=dense_config.on_disk,
        )

        # Create isolation indexes (CRITICAL)
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="workspace_id",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        await client.create_payload_index(
            collection_name=collection_name,
            field_name="context_id",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        await client.create_payload_index(
            collection_name=collection_name,
            field_name="user_id",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        # Create full-text indexes
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="summary",
            field_schema=TextIndexParams(  # type: ignore[arg-type]
                type="text",  # type: ignore[arg-type]
                tokenizer=TokenizerType.MULTILINGUAL,
                min_token_len=2,
                max_token_len=20,
                lowercase=True,
            ),
        )

        await client.create_payload_index(
            collection_name=collection_name,
            field_name="context_summary",
            field_schema=TextIndexParams(  # type: ignore[arg-type]
                type="text",  # type: ignore[arg-type]
                tokenizer=TokenizerType.MULTILINGUAL,
                min_token_len=2,
                max_token_len=20,
                lowercase=True,
            ),
        )

        # Create pre-tokenized text indexes (Issue #1: Japanese BM25)
        # These fields store Sudachi-lemmatized tokens for accurate Japanese search
        for field in ("summary_tokens", "context_summary_tokens", "content_tokens"):
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=TextIndexParams(  # type: ignore[arg-type]
                    type="text",  # type: ignore[arg-type]
                    tokenizer=TokenizerType.WORD,
                    min_token_len=1,
                    max_token_len=30,
                    lowercase=True,
                ),
            )

        # Create keyword indexes
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="scope",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        await client.create_payload_index(
            collection_name=collection_name,
            field_name="type",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        # Create float index for importance range filtering
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="importance",
            field_schema="float",  # type: ignore[arg-type]
        )

        # Issue #67: Keyword index for tag filtering (exact match)
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="tags",
            field_schema="keyword",  # type: ignore[arg-type]
        )

        # Issue #78: Datetime indexes for date range filtering
        for dt_field in ("created_at", "updated_at"):
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=dt_field,
                field_schema="datetime",  # type: ignore[arg-type]
            )

        # WHERE axis (#1332): geo index for filters.near (geo_radius)
        await client.create_payload_index(
            collection_name=collection_name,
            field_name="location",
            field_schema="geo",  # type: ignore[arg-type]
        )

        # #2015: content chunk points carry chunk_of (the memory id)
        await client.create_payload_index(
            collection_name=collection_name,
            field_name=CHUNK_OF_FIELD,
            field_schema="keyword",  # type: ignore[arg-type]
        )

        logger.info(
            "single_collection_indexes_created",
            collection=collection_name,
            indexes=[
                "workspace_id",
                "context_id",
                "user_id",
                "summary",
                "context_summary",
                "scope",
                "type",
                "importance",
                "tags",
                "created_at",
                "updated_at",
                CHUNK_OF_FIELD,
            ],
        )

    except Exception as e:
        logger.error(
            "single_collection_creation_failed",
            collection=collection_name,
            error=str(e),
        )
        raise QdrantError(f"Failed to create kagura_memories collection: {e}") from e


async def copy_context_points(
    workspace_id: str,
    source_context_id: str,
    target_context_id: str,
    memory_id_mapping: dict[str, str],
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
    batch_size: int = 100,
) -> set[str]:
    """Copy Qdrant points from source context to target context (Issue #90).

    Retrieves points by ID, creates new points with updated context_id and new IDs.
    Processes in batches for memory efficiency.

    #1497: returns the NEW ids actually upserted, not a count. Two branches here
    skip a memory silently — a source point that does not exist, and one stored
    without a vector — and a bare count cannot say WHICH. The caller needs that
    to avoid marking a target row ``success`` when nothing was copied for it,
    which would manufacture a row no automatic path can ever repair.

    Args:
        workspace_id: Workspace ID
        source_context_id: Source context ID
        target_context_id: Target context ID
        memory_id_mapping: {old_memory_id -> new_memory_id} mapping
        collection_name: Qdrant collection name
        batch_size: Points per batch

    Returns:
        The set of new memory ids whose vector was upserted. A caller comparing
        this against ``memory_id_mapping.values()`` learns exactly which rows
        have no vector in the target.

    Raises:
        QdrantError: If copy fails
    """
    if _active_store() is not None:
        raise NotImplementedError(
            "copy_context_points is not supported by the LanceDB backend (preview)."
        )

    client = get_qdrant_client()
    upserted: set[str] = set()

    try:
        old_ids = list(memory_id_mapping.keys())

        for i in range(0, len(old_ids), batch_size):
            batch_ids = old_ids[i : i + batch_size]

            # Fetch existing points with vectors
            points = await client.retrieve(
                collection_name=collection_name,
                ids=batch_ids,
                with_vectors=True,
                with_payload=True,
            )

            if not points:
                continue

            # Build new points with updated context_id and new IDs
            new_points = []
            for point in points:
                old_id = str(point.id)
                new_id = memory_id_mapping.get(old_id)
                if not new_id:
                    continue
                if point.vector is None:
                    logger.warning("skipping_point_without_vector", point_id=old_id)
                    continue

                new_payload = dict(point.payload) if point.payload else {}
                new_payload["context_id"] = target_context_id
                # Keep nested context metadata consistent
                if isinstance(new_payload.get("context"), dict):
                    new_payload["context"] = {
                        **new_payload["context"],
                        "context_id": target_context_id,
                    }

                new_points.append(
                    PointStruct(
                        id=new_id,
                        vector=point.vector,
                        payload=new_payload,
                    )
                )

            if new_points:
                await client.upsert(
                    collection_name=collection_name,
                    points=new_points,
                )
                upserted.update(str(p.id) for p in new_points)

            logger.debug(
                "context_points_copy_batch",
                batch=i // batch_size + 1,
                copied_in_batch=len(new_points),
                total_copied=len(upserted),
            )

        logger.info(
            "context_points_copied",
            collection=collection_name,
            workspace_id=workspace_id,
            source_context_id=source_context_id,
            target_context_id=target_context_id,
            count=len(upserted),
        )

        return upserted

    except Exception as e:
        logger.error(
            "context_points_copy_failed",
            source_context_id=source_context_id,
            target_context_id=target_context_id,
            error=str(e),
        )
        raise QdrantError(f"Failed to copy context points: {e}") from e


async def delete_context_points(
    workspace_id: str, context_id: str, collection_name: str = KAGURA_MEMORIES_COLLECTION
) -> int:
    """Delete all points for a specific context from kagura_memories collection.

    This is used when a context is deleted. Instead of deleting an entire collection
    (old design), we now delete all points matching workspace_id + context_id.

    Args:
        workspace_id: Workspace ID
        context_id: Context ID

    Returns:
        Number of points deleted (counted before deletion for verification)

    Raises:
        QdrantError: If deletion fails

    Example:
        >>> deleted = await delete_context_points("workspace-uuid", "context-uuid")
        >>> print(f"Deleted {deleted} points")
    """
    _store = _active_store()
    if _store is not None:
        return await _store.delete_context_points(workspace_id, context_id, collection_name)

    client = get_qdrant_client()

    try:
        # Build filter for workspace_id + context_id
        filter_conditions = Filter(
            must=[
                FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                FieldCondition(key="context_id", match=MatchValue(value=context_id)),
            ]
        )

        # Issue #273 Review: Count before delete for verification
        # Prevents silent failures - caller can verify deletion succeeded
        count_result = await client.count(
            collection_name=collection_name,
            count_filter=filter_conditions,
        )
        points_to_delete = count_result.count

        # Delete points matching the filter
        await client.delete(
            collection_name=collection_name,
            points_selector=filter_conditions,
        )

        logger.info(
            "context_points_deleted",
            collection=collection_name,
            workspace_id=workspace_id,
            context_id=context_id,
            count=points_to_delete,
        )

        return points_to_delete

    except Exception as e:
        logger.error(
            "context_points_deletion_failed",
            collection=collection_name,
            workspace_id=workspace_id,
            context_id=context_id,
            error=str(e),
        )
        raise QdrantError(f"Failed to delete context points: {e}") from e


async def list_context_point_ids(
    workspace_id: str, context_id: str, collection_name: str, *, page_size: int = 1000
) -> list[str]:
    """Every point id stored for a context in ``collection_name`` (#1525).

    Qdrant only — scrolls by the same ``workspace_id`` + ``context_id`` payload
    filter :func:`delete_context_points` deletes by, without payload or
    vectors. Used by the embedding migration to find points the target
    collection holds for memories that are no longer live. Content chunk
    points (#2015) are left out: their ids are never memory ids, so the
    verify pass must not take them for stale points.

    Raises:
        QdrantError: If the scroll fails.
    """
    client = get_qdrant_client()
    filter_conditions = summary_points_filter(
        FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
        FieldCondition(key="context_id", match=MatchValue(value=context_id)),
    )
    ids: list[str] = []
    offset = None
    try:
        while True:
            points, offset = await client.scroll(
                collection_name=collection_name,
                scroll_filter=filter_conditions,
                limit=page_size,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids.extend(str(point.id) for point in points)
            if offset is None or not points:
                return ids
    except Exception as e:
        raise QdrantError(f"Failed to list context points: {e}") from e


async def delete_points_from_qdrant(point_ids: list[str], collection_name: str) -> None:
    """Delete ``point_ids`` from ``collection_name`` in one request (#1525).

    Ids that do not exist are ignored by Qdrant, so callers may pass a
    superset. No-op on an empty list.

    Raises:
        QdrantError: If deletion fails.
    """
    if not point_ids:
        return
    _store = _active_store()
    if _store is not None:
        await _store.delete_points(list(point_ids), collection_name)
        return

    client = get_qdrant_client()
    try:
        await client.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=list(point_ids)),
        )
        logger.info("points_deleted_from_qdrant", collection=collection_name, count=len(point_ids))
    except Exception as e:
        raise QdrantError(f"Failed to delete points: {e}") from e


async def list_memory_collections() -> list[str]:
    """Every ``kagura_memories*`` collection: the default and the per-model variants (#1798).

    Raises:
        QdrantError: If the collections cannot be listed.
    """
    _store = _active_store()
    if _store is not None:
        return await _store.list_collections()

    client = get_qdrant_client()
    try:
        response = await client.get_collections()
    except Exception as e:
        raise QdrantError(f"Failed to list collections: {e}") from e
    return sorted(
        c.name for c in response.collections if c.name.startswith(KAGURA_MEMORIES_COLLECTION)
    )


async def ensure_chunk_of_index_on_all() -> list[str]:
    """Add the ``chunk_of`` payload index (#2015) to every ``kagura_memories*``
    collection that lacks it; return the collections it was added to.

    Every dense search, point delete and payload update now filters on
    ``chunk_of``, so a collection created before #2015 needs the index even
    when it never holds a chunk point. Startup pass, once per process:
    idempotent (an existing index is left alone; Qdrant also accepts a
    repeated create) and fail-soft — a collection that fails is logged and
    skipped, and nothing is raised. A no-op on a non-Qdrant store.
    """
    if _active_store() is not None:
        return []
    created: list[str] = []
    try:
        names = await list_memory_collections()
    except Exception as e:  # noqa: BLE001 — best-effort startup pass
        logger.warning("chunk_of_index_pass_failed", error_class=type(e).__name__)
        return created
    client = get_qdrant_client()
    for name in names:
        try:
            info = await client.get_collection(name)
            existing = set(info.payload_schema.keys()) if info.payload_schema else set()
            if CHUNK_OF_FIELD in existing:
                continue
            await client.create_payload_index(
                collection_name=name,
                field_name=CHUNK_OF_FIELD,
                field_schema="keyword",  # type: ignore[arg-type]
            )
            created.append(name)
            logger.info("created_missing_index", field=CHUNK_OF_FIELD, collection=name)
        except Exception as e:  # noqa: BLE001 — one collection must not stop the pass
            logger.warning(
                "chunk_of_index_create_failed", collection=name, error_class=type(e).__name__
            )
    return created


async def delete_context_points_everywhere(
    workspace_id: str, context_id: str, *, collections: list[str] | None = None
) -> dict[str, int]:
    """Delete a context's points from every ``kagura_memories*`` collection (#1798).

    A context routes to one collection, but an embedding migration leaves its
    points in a second one until the source is purged, and a deleted context
    has no use for either. Asking every collection also means the caller does
    not have to resolve the routing of a context it is removing.

    Args:
        collections: The collections to ask, for a caller that deletes many
            contexts and has listed them once (default: list them now).

    Returns:
        Collection name -> points deleted, for the collections that held any.

    Raises:
        QdrantError: If listing fails, or — after every collection has been
            tried — if any single deletion failed.
    """
    names = collections if collections is not None else await list_memory_collections()
    deleted: dict[str, int] = {}
    failed: list[str] = []
    for collection_name in names:
        try:
            count = await delete_context_points(workspace_id, context_id, collection_name)
        except QdrantError as e:
            # The collection that actually holds the points may come later.
            failed.append(f"{collection_name}: {e}")
            continue
        if count:
            deleted[collection_name] = count
    if failed:
        raise QdrantError(
            f"Failed to delete context points from {len(failed)} collection(s) "
            f"(deleted {sum(deleted.values())} elsewhere): {'; '.join(failed)}"
        )
    return deleted


@dataclass(frozen=True)
class PointRef:
    """What the orphan sweep needs to know about one stored point (#1798)."""

    point_id: str
    context_id: str | None
    # A resource-indexer point: its id is derived from the document, not from
    # a memory row, so it cannot be matched to ``memories.id`` — only to
    # ``memories.summary_embedding_id`` (#1829).
    is_resource: bool
    # Resource points only: the document the id was derived from
    # (resource_id, doc_id, version), so a point whose row was purged can
    # still be checked against the natural key before it is deleted (#1829).
    resource_key: tuple[str, str, int] | None = None
    # Resource points only: when the point was written, so a point whose row
    # has not committed yet is never taken for an orphan. Read from the payload
    # ``indexed_at`` (#1869), or ``updated_at`` if that is later; a point
    # written by an older release has only
    # ``updated_at`` — the event time, which can be long before the write —
    # and falls back to it, so a row-less point such a release left behind is
    # still swept. A missing ``indexed_at`` cannot tell a historical point
    # from one an older release is writing right now: while a rollout still
    # has such a process indexing a backlog, its in-flight points get the
    # pre-#1869 behaviour (no worse than before), and only for that overlap.
    written_at: datetime | None = None
    # A content chunk point (#2015): the memory id it belongs to. Its own id
    # is derived from (memory id, chunk index), so the row is ``chunk_of``.
    chunk_of: str | None = None


def _payload_datetime(raw: Any) -> datetime | None:
    """A payload ISO timestamp as naive UTC, or ``None`` if it is not one."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, OverflowError):
        # OverflowError: an offset that pushes the value out of datetime's range.
        return None


def _point_ref(point: Any) -> PointRef:
    """A :class:`PointRef` for one scrolled point (#1798, #1829)."""
    payload = point.payload or {}
    is_resource = "resource_id" in payload
    resource_key: tuple[str, str, int] | None = None
    written_at: datetime | None = None
    if is_resource:
        doc_id, version = payload.get("doc_id"), payload.get("version")
        if isinstance(doc_id, str) and isinstance(version, int) and not isinstance(version, bool):
            resource_key = (str(payload["resource_id"]), doc_id, version)
        # The later of the two: ``indexed_at`` only ever keeps a point longer
        # than ``updated_at`` alone did (an event dated ahead of this host's
        # clock must not make its point look older than before #1869).
        stamps = [
            stamp
            for stamp in (
                _payload_datetime(payload.get("indexed_at")),
                _payload_datetime(payload.get("updated_at")),
            )
            if stamp is not None
        ]
        written_at = max(stamps) if stamps else None
    chunk_of = payload.get(CHUNK_OF_FIELD)
    return PointRef(
        point_id=str(point.id),
        context_id=payload.get("context_id"),
        is_resource=is_resource,
        resource_key=resource_key,
        written_at=written_at,
        chunk_of=str(chunk_of) if chunk_of else None,
    )


async def scroll_point_refs(
    collection_name: str, *, page_size: int = 1000
) -> AsyncIterator[list[PointRef]]:
    """Yield every point of ``collection_name`` as :class:`PointRef` pages (#1798).

    Crosses workspace and context boundaries on purpose — the orphan sweep is
    a deployment-wide maintenance read, and it returns ids only (no content,
    no vectors). Do not use it from a request path.

    Raises:
        QdrantError: If a page cannot be read.
    """
    _store = _active_store()
    if _store is not None:
        # Every row of an alternative store is a memory point: the resource
        # indexer writes through the Qdrant client only. A store that gains
        # resource points must report them here, or the sweep would judge
        # them by a memory row they never had.
        refs = [
            PointRef(point_id=point_id, context_id=context_id, is_resource=False)
            for point_id, context_id in await _store.list_point_refs(collection_name)
        ]
        for start in range(0, len(refs), page_size):
            yield refs[start : start + page_size]
        return

    client = get_qdrant_client()
    from qdrant_client.conversions.common_types import PointId

    offset: PointId | None = None
    while True:
        try:
            points, offset = await client.scroll(
                collection_name=collection_name,
                limit=page_size,
                offset=offset,
                with_payload=[
                    "context_id",
                    "resource_id",
                    "doc_id",
                    "version",
                    "updated_at",
                    "indexed_at",
                    CHUNK_OF_FIELD,
                ],
                with_vectors=False,
            )
        except Exception as e:
            raise QdrantError(f"Failed to scroll {collection_name}: {e}") from e
        if points:
            yield [_point_ref(point) for point in points]
        if offset is None or not points:
            return


async def delete_user_points(user_id: str) -> dict[str, int]:
    """Delete every point authored by a user across all kagura_memories collections.

    Issue #360 (GDPR Art.17 / APPI 第22条): the user-scoped variant of
    ``delete_context_points``. Filters by ``user_id`` alone — deliberately
    crossing workspace + context boundaries because the GDPR data subject
    is the *author* of the point, not the workspace owner. This means
    points the user authored inside a shared workspace they were a member
    of (but did not own) are also deleted; this is the correct
    interpretation of "right to erasure" and is documented in the ops
    runbook so it is not a surprise to workspace co-owners.

    Iterates every collection whose name begins with ``kagura_memories``
    so that per-model variant collections (``kagura_memories_<slug>_<dim>``,
    see ``get_collection_name``) are covered without the caller having
    to enumerate them.

    Args:
        user_id: OAuth2 ``sub`` of the user being erased.

    Returns:
        Dict mapping collection name to deleted point count.

    Raises:
        QdrantError: If listing collections or deleting from any single
            collection fails. The error message includes the offending
            collection so reconciliation knows where to retry.

    Example:
        >>> deleted = await delete_user_points("google-oauth2|123")
        >>> sum(deleted.values())
        42
        >>> deleted
        {'kagura_memories': 38, 'kagura_memories_voyage_2_1024': 4}
    """
    _store = _active_store()
    if _store is not None:
        # #1336: the Lance backend now implements the cross-collection GDPR
        # erasure — the preview NotImplementedError left erased users'
        # vectors at rest on self-hosted Lite installs.
        return await _store.delete_user_points(user_id)

    client = get_qdrant_client()
    deleted_per_collection: dict[str, int] = {}

    try:
        collections_response = await client.get_collections()
        target_collections = [
            c.name
            for c in collections_response.collections
            if c.name.startswith(KAGURA_MEMORIES_COLLECTION)
        ]
    except Exception as e:
        logger.error("user_points_list_collections_failed", user_id=user_id, error=str(e))
        raise QdrantError(f"Failed to list collections for user erasure: {e}") from e

    user_filter = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))])

    for collection_name in target_collections:
        try:
            count_result = await client.count(
                collection_name=collection_name,
                count_filter=user_filter,
            )
            points_to_delete = count_result.count

            if points_to_delete > 0:
                await client.delete(
                    collection_name=collection_name,
                    points_selector=user_filter,
                )

            deleted_per_collection[collection_name] = points_to_delete
            logger.info(
                "user_points_deleted",
                collection=collection_name,
                user_id=user_id,
                count=points_to_delete,
            )
        except Exception as e:
            logger.error(
                "user_points_deletion_failed",
                collection=collection_name,
                user_id=user_id,
                error=str(e),
            )
            raise QdrantError(f"Failed to delete user points from {collection_name}: {e}") from e

    return deleted_per_collection


# Payload field names that memory_service writes per point (issue #16),
# read by the BM25 drift reveal-terms endpoint (#377). Source of truth so
# writer (services/memory_service.py) and reader cannot drift apart.
QDRANT_TOKEN_PAYLOAD_FIELDS: tuple[str, ...] = (
    "summary_tokens",
    "context_summary_tokens",
    "content_tokens",
    "summary_reading",
)


async def _admin_scroll_context_points(
    context_id: str,
    *,
    with_vectors: bool = False,
    with_payload: bool | list[str] = True,
    collection_name: str = KAGURA_MEMORIES_COLLECTION,
) -> AsyncIterator[list]:
    """Yield Qdrant point pages for a context, one page at a time. ADMIN-ONLY.

    The leading underscore + `_admin_` prefix signal that this helper
    deliberately skips workspace + user isolation so every point in the
    context is returned regardless of ownership. New callers must opt
    into the isolation bypass explicitly — do NOT use this from any
    non-admin code path.

    Used by the BM25 drift reveal-terms endpoint (#377) to rebuild the
    token → mmh3-hash mapping for reverse lookup. Summary (and resource)
    points only: content chunk points (#2015) carry no token fields (#2041).

    Yields pages (lists of Record) instead of materializing the full
    point set so callers can break early once they have enough data —
    a 100K-point context yields ~500 pages of 200, but the caller
    typically needs only a handful of points to satisfy a reverse-lookup.

    Args:
        context_id: Context UUID string to filter on.
        with_vectors: If True, include dense + sparse vectors in results.
        with_payload: True for full payload, list[str] to fetch only
            the named keys (recommended on hot paths to cut bandwidth).
        collection_name: Qdrant collection name.

    Yields:
        Successive lists of qdrant_client.models.Record, one per scroll page.
    """
    if _active_store() is not None:
        raise NotImplementedError(
            "_admin_scroll_context_points (BM25 drift admin tool) is not supported "
            "by the LanceDB backend (preview)."
        )

    client = get_qdrant_client()
    # qdrant-client's PointId union (int | str | UUID) matches scroll's
    # declared offset parameter and the next_offset return value.
    from qdrant_client.conversions.common_types import PointId

    offset: PointId | None = None

    while True:
        points, next_offset = await client.scroll(
            collection_name=collection_name,
            # #2041: chunk points carry no BM25 token fields; their summary is
            # their memory's, so they could only repeat a summary point's tokens.
            scroll_filter=summary_points_filter(
                FieldCondition(key="context_id", match=MatchValue(value=context_id))
            ),
            limit=200,
            offset=offset,
            with_payload=with_payload,
            with_vectors=with_vectors,
        )
        yield points
        if next_offset is None:
            break
        offset = next_offset
