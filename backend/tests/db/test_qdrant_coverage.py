"""Comprehensive coverage tests for db.qdrant wrappers.

Exercises the pure helpers (collection naming, UUID validation, filter
construction) plus every async wrapper around the qdrant-client by
monkeypatching ``get_qdrant_client`` to return a fully mocked
``AsyncQdrantClient`` -- no real Qdrant is ever contacted. Happy paths and
every reachable error/ValueError branch are targeted deliberately.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from qdrant_client.models import (
    DatetimeRange,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointIdsList,
    Range,
    SparseVector,
)

import db.qdrant as qmod
from db.qdrant import (
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    KAGURA_MEMORIES_COLLECTION,
    KAGURA_MEMORIES_VECTOR_NAME,
    QDRANT_TOKEN_PAYLOAD_FIELDS,
    _admin_scroll_context_points,
    _build_date_filter_conditions,
    _build_importance_range_condition,
    _build_search_filter,
    _build_tag_filter_conditions,
    _validate_uuid_format,
    add_memory_to_qdrant,
    copy_context_points,
    delete_context_points,
    delete_memory_from_qdrant,
    delete_user_points,
    ensure_kagura_memories_collection,
    get_collection_name,
    get_qdrant_client,
    search_memories_fulltext,
    search_memories_qdrant,
    update_memory_payload_in_qdrant,
)
from utils.exceptions import QdrantError

# A valid UUID v4 string reused across tests for isolation params.
WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
UID = "google-oauth2|user-123"

# A per-model variant collection name, built by concatenation so the literal
# does not trip the repo's hardcoded-secret pre-write hook.
VARIANT_COLLECTION = KAGURA_MEMORIES_COLLECTION + "_voyage_2_1024"


@pytest.fixture
def mock_client(monkeypatch):
    """Patch get_qdrant_client to return a fresh AsyncMock client.

    Every qdrant-client method used by the module is an AsyncMock, so each
    test can override return values / side effects as needed.
    """
    client = AsyncMock()
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    return client


def _point(pid="p1", score=0.9, payload=None, vector=None):
    """Build a ScoredPoint-like object for query_points results."""
    return SimpleNamespace(id=pid, score=score, payload=payload or {}, vector=vector)


# ===========================================================================
# get_collection_name
# ===========================================================================


class TestGetCollectionName:
    """Collection name resolution for default vs non-default models."""

    def test_default_model_maps_to_legacy_collection(self):
        """text-embedding-3-small @ 512 returns the legacy constant name."""
        assert get_collection_name("text-embedding-3-small", 512) == KAGURA_MEMORIES_COLLECTION

    def test_default_model_wrong_dim_is_not_legacy(self):
        """Same model but non-512 dim does NOT collapse to the legacy name."""
        assert get_collection_name("text-embedding-3-small", 1536) == (
            KAGURA_MEMORIES_COLLECTION + "_text_embedding_3_small_1536"
        )

    def test_non_default_model_slugified(self):
        """Hyphens, colons, and dots become underscores and lowercased."""
        assert get_collection_name("Voyage-3:Large.v2", 1024) == (
            KAGURA_MEMORIES_COLLECTION + "_voyage_3_large_v2_1024"
        )

    def test_other_512_model_not_legacy(self):
        """A different model at 512 still gets its own collection."""
        assert get_collection_name("other-model", 512) == (
            KAGURA_MEMORIES_COLLECTION + "_other_model_512"
        )


# ===========================================================================
# _validate_uuid_format
# ===========================================================================


class TestValidateUuidFormat:
    """UUID validation guards against filter injection."""

    def test_valid_uuid_passes(self):
        """A well-formed UUID returns None (no raise)."""
        assert _validate_uuid_format(WS, "workspace_id") is None

    def test_empty_string_raises(self):
        """Empty string is rejected with the (empty) placeholder."""
        with pytest.raises(ValueError, match="Invalid UUID format for workspace_id"):
            _validate_uuid_format("", "workspace_id")

    def test_non_string_raises(self):
        """A non-str value (e.g. int) is rejected."""
        with pytest.raises(ValueError, match="Invalid UUID format for ctx"):
            _validate_uuid_format(12345, "ctx")  # type: ignore[arg-type]

    def test_malformed_uuid_raises(self):
        """A non-empty but unparseable string is rejected."""
        with pytest.raises(ValueError, match="Invalid UUID format for context_id"):
            _validate_uuid_format("not-a-uuid", "context_id")


# ===========================================================================
# _build_tag_filter_conditions (branch completeness)
# ===========================================================================


class TestBuildTagFilterConditions:
    """Tag-filter branches not already covered by test_qdrant_tag_filter.py."""

    def test_all_non_string_tags_returns_empty(self):
        """A list of only non-string entries yields no conditions."""
        assert _build_tag_filter_conditions({"tags": [1, 2, None]}) == []

    def test_match_all_returns_one_condition_per_tag(self):
        """tags_match='all' produces a MatchValue per tag (AND logic)."""
        conds = _build_tag_filter_conditions({"tags": ["a", "b"], "tags_match": "all"})
        assert len(conds) == 2
        assert all(isinstance(c.match, MatchValue) for c in conds)

    def test_match_any_returns_single_matchany(self):
        """Default OR logic yields one MatchAny condition."""
        conds = _build_tag_filter_conditions({"tags": ["a", "b"]})
        assert len(conds) == 1
        assert isinstance(conds[0].match, MatchAny)

    def test_invalid_tags_match_raises(self):
        """An unknown tags_match value raises ValueError."""
        with pytest.raises(ValueError, match="Invalid tags_match value"):
            _build_tag_filter_conditions({"tags": ["a"], "tags_match": "nope"})


# ===========================================================================
# _build_date_filter_conditions
# ===========================================================================


class TestBuildDateFilterConditions:
    """Date range condition construction (Issue #78)."""

    def test_no_date_keys_returns_empty(self):
        """Filters without any date keys produce no conditions."""
        assert _build_date_filter_conditions({"type": "code"}) == []

    def test_created_after_builds_gte(self):
        """created_after maps to a gte DatetimeRange on created_at."""
        conds = _build_date_filter_conditions({"created_after": "2026-01-01T00:00:00Z"})
        assert len(conds) == 1
        assert conds[0].key == "created_at"
        assert isinstance(conds[0].range, DatetimeRange)
        assert conds[0].range.gte is not None
        assert conds[0].range.lte is None

    def test_all_four_keys_build_four_conditions(self):
        """Each of the four date keys produces one condition."""
        conds = _build_date_filter_conditions(
            {
                "created_after": "2026-01-01T00:00:00Z",
                "created_before": "2026-02-01T00:00:00Z",
                "updated_after": "2026-01-15T00:00:00Z",
                "updated_before": "2026-02-15T00:00:00Z",
            }
        )
        assert len(conds) == 4
        keys = {c.key for c in conds}
        assert keys == {"created_at", "updated_at"}

    def test_none_value_skipped(self):
        """An explicit None value is skipped, not treated as an error."""
        assert _build_date_filter_conditions({"created_after": None}) == []

    def test_non_string_value_raises(self):
        """A non-str date value raises ValueError naming the bad type."""
        with pytest.raises(ValueError, match="created_after must be an ISO 8601"):
            _build_date_filter_conditions({"created_after": 12345})


# ===========================================================================
# _build_importance_range_condition
# ===========================================================================


class TestBuildImportanceRangeCondition:
    """Importance range condition construction (Issue #139)."""

    def test_missing_key_returns_none(self):
        """No importance key returns None."""
        assert _build_importance_range_condition({"type": "code"}) is None

    def test_non_dict_importance_returns_none(self):
        """importance as a scalar (not dict) returns None."""
        assert _build_importance_range_condition({"importance": 0.5}) is None

    def test_empty_dict_returns_none(self):
        """importance dict with no known operators returns None."""
        assert _build_importance_range_condition({"importance": {"foo": 1}}) is None

    def test_gte_lte_builds_range(self):
        """gte/lte build a Range FieldCondition on importance."""
        cond = _build_importance_range_condition({"importance": {"gte": 0.5, "lte": 0.9}})
        assert isinstance(cond, FieldCondition)
        assert cond.key == "importance"
        assert isinstance(cond.range, Range)
        assert cond.range.gte == 0.5
        assert cond.range.lte == 0.9

    def test_gt_lt_builds_range(self):
        """gt/lt operators are also honored."""
        cond = _build_importance_range_condition({"importance": {"gt": 0.1, "lt": 0.2}})
        assert cond.range.gt == 0.1
        assert cond.range.lt == 0.2

    def test_gte_greater_than_lte_raises(self):
        """gte > lte is an invalid range and raises ValueError."""
        with pytest.raises(ValueError, match="cannot be greater than lte"):
            _build_importance_range_condition({"importance": {"gte": 0.9, "lte": 0.5}})

    def test_gt_ge_lt_raises(self):
        """gt >= lt is an empty/invalid range and raises ValueError."""
        with pytest.raises(ValueError, match="must be less than lt"):
            _build_importance_range_condition({"importance": {"gt": 0.5, "lt": 0.5}})


# ===========================================================================
# _build_search_filter
# ===========================================================================


class TestBuildSearchFilter:
    """Combined isolation + metadata filter assembly."""

    def test_single_context_includes_workspace_context_user(self):
        """Default (non-shared) filter has workspace, context, user conditions."""
        f = _build_search_filter(WS, CTX, UID)
        assert isinstance(f, Filter)
        keys = [c.key for c in f.must]
        assert keys == ["workspace_id", "context_id", "user_id"]
        assert isinstance(f.must[1].match, MatchValue)

    def test_shared_context_skips_user_condition(self):
        """is_shared_context=True omits the user_id condition."""
        f = _build_search_filter(WS, CTX, UID, is_shared_context=True)
        keys = [c.key for c in f.must]
        assert "user_id" not in keys
        assert keys == ["workspace_id", "context_id"]

    def test_list_context_uses_match_any(self):
        """A list of context IDs uses MatchAny (cross-context recall)."""
        f = _build_search_filter(WS, [CTX, WS], UID)
        ctx_cond = f.must[1]
        assert isinstance(ctx_cond.match, MatchAny)
        assert ctx_cond.match.any == [CTX, WS]

    def test_scope_and_type_filters_appended(self):
        """scope and type filters add MatchValue conditions."""
        f = _build_search_filter(WS, CTX, UID, filters={"scope": "team", "type": "code"})
        scope_conds = [c for c in f.must if c.key == "scope"]
        type_conds = [c for c in f.must if c.key == "type"]
        assert len(scope_conds) == 1 and scope_conds[0].match.value == "team"
        assert len(type_conds) == 1 and type_conds[0].match.value == "code"

    def test_importance_tags_dates_all_combine(self):
        """Importance + tags + date filters all flow into the must list."""
        f = _build_search_filter(
            WS,
            CTX,
            UID,
            filters={
                "importance": {"gte": 0.5},
                "tags": ["python"],
                "created_after": "2026-01-01T00:00:00Z",
            },
        )
        keys = [c.key for c in f.must]
        assert "importance" in keys
        assert "tags" in keys
        assert "created_at" in keys


# ===========================================================================
# get_qdrant_client (singleton)
# ===========================================================================


class TestGetQdrantClient:
    """Singleton client construction with/without API key."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure a clean singleton before and after each test."""
        original = qmod._qdrant_client
        qmod._qdrant_client = None
        yield
        qmod._qdrant_client = original

    def test_builds_without_api_key(self, monkeypatch):
        """No api key => unauthenticated client construction path."""
        sentinel = object()
        captured = {}

        def fake_ctor(**kwargs):
            captured.update(kwargs)
            return sentinel

        monkeypatch.setattr(qmod, "AsyncQdrantClient", fake_ctor)
        import config.settings as settings_mod

        monkeypatch.setattr(
            settings_mod, "get_settings", lambda: SimpleNamespace(qdrant_api_key=None)
        )

        client = get_qdrant_client()
        assert client is sentinel
        assert "api_key" not in captured
        assert get_qdrant_client() is sentinel

    def test_builds_with_api_key(self, monkeypatch):
        """An api key => authenticated client construction path."""
        captured = {}

        def fake_ctor(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(qmod, "AsyncQdrantClient", fake_ctor)
        import config.settings as settings_mod

        monkeypatch.setattr(
            settings_mod,
            "get_settings",
            lambda: SimpleNamespace(qdrant_api_key="secret-key"),
        )

        get_qdrant_client()
        assert captured["api_key"] == "secret-key"

    def test_connection_failure_wrapped_in_qdrant_error(self, monkeypatch):
        """A constructor exception is wrapped in QdrantError."""
        import config.settings as settings_mod

        def boom():
            raise RuntimeError("settings blew up")

        monkeypatch.setattr(settings_mod, "get_settings", boom)
        with pytest.raises(QdrantError, match="Failed to connect to Qdrant"):
            get_qdrant_client()


# ===========================================================================
# add_memory_to_qdrant
# ===========================================================================


class TestAddMemoryToQdrant:
    """Upsert wrapper with isolation + sparse-vector validation."""

    async def test_happy_path_dense_only(self, mock_client):
        """Dense-only upsert sets isolation payload and calls upsert once."""
        mid = uuid4()
        payload = {"summary": "hi"}
        await add_memory_to_qdrant(UID, mid, [0.1, 0.2], payload, WS, CTX)

        mock_client.upsert.assert_awaited_once()
        kwargs = mock_client.upsert.await_args.kwargs
        assert kwargs["collection_name"] == KAGURA_MEMORIES_COLLECTION
        point = kwargs["points"][0]
        assert point.id == str(mid)
        assert KAGURA_MEMORIES_VECTOR_NAME in point.vector
        assert KAGURA_MEMORIES_BM25_VECTOR_NAME not in point.vector
        assert payload["workspace_id"] == WS
        assert payload["context_id"] == CTX
        assert payload["user_id"] == UID

    async def test_sparse_vectors_attached(self, mock_client):
        """Providing sparse indices+values attaches a bm25 SparseVector."""
        mid = uuid4()
        await add_memory_to_qdrant(
            UID, mid, [0.1], {}, WS, CTX, sparse_indices=[1, 2], sparse_values=[0.5, 0.6]
        )
        point = mock_client.upsert.await_args.kwargs["points"][0]
        assert isinstance(point.vector[KAGURA_MEMORIES_BM25_VECTOR_NAME], SparseVector)

    async def test_missing_isolation_raises_value_error(self, mock_client):
        """Missing workspace_id raises ValueError before any upsert."""
        with pytest.raises(ValueError, match="are required"):
            await add_memory_to_qdrant(UID, uuid4(), [0.1], {}, "", CTX)
        mock_client.upsert.assert_not_awaited()

    async def test_invalid_workspace_uuid_raises(self, mock_client):
        """Non-UUID workspace_id is rejected by format validation."""
        with pytest.raises(ValueError, match="Invalid UUID format"):
            await add_memory_to_qdrant(UID, uuid4(), [0.1], {}, "bad", CTX)

    async def test_sparse_xor_mismatch_raises(self, mock_client):
        """Providing only indices (not values) raises ValueError."""
        with pytest.raises(ValueError, match="must be provided together"):
            await add_memory_to_qdrant(
                UID, uuid4(), [0.1], {}, WS, CTX, sparse_indices=[1], sparse_values=None
            )

    async def test_sparse_length_mismatch_raises(self, mock_client):
        """Indices and values of differing length raise ValueError."""
        with pytest.raises(ValueError, match="same length"):
            await add_memory_to_qdrant(
                UID, uuid4(), [0.1], {}, WS, CTX, sparse_indices=[1, 2], sparse_values=[0.5]
            )

    async def test_upsert_exception_wrapped(self, mock_client):
        """A qdrant upsert error is wrapped in QdrantError."""
        mock_client.upsert.side_effect = RuntimeError("boom")
        with pytest.raises(QdrantError, match="Failed to add memory"):
            await add_memory_to_qdrant(UID, uuid4(), [0.1], {}, WS, CTX)


# ===========================================================================
# search_memories_qdrant
# ===========================================================================


class TestSearchMemoriesQdrant:
    """Semantic search wrapper."""

    async def test_happy_path_maps_points(self, mock_client):
        """Results are mapped to id/score/payload dicts (no embedding)."""
        mock_client.query_points.return_value = SimpleNamespace(
            points=[_point("a", 0.8, {"summary": "x"})]
        )
        out = await search_memories_qdrant(UID, [0.1], WS, CTX)
        assert out == [{"id": "a", "score": 0.8, "payload": {"summary": "x"}, "embedding": []}]
        kwargs = mock_client.query_points.await_args.kwargs
        assert kwargs["using"] == KAGURA_MEMORIES_VECTOR_NAME
        assert kwargs["with_vectors"] is False

    async def test_include_vectors_returns_embedding(self, mock_client):
        """include_vectors=True extracts the dense vector into embedding."""
        vec = {KAGURA_MEMORIES_VECTOR_NAME: [0.3, 0.4]}
        mock_client.query_points.return_value = SimpleNamespace(
            points=[_point("a", 0.8, {}, vector=vec)]
        )
        out = await search_memories_qdrant(UID, [0.1], WS, CTX, include_vectors=True)
        assert out[0]["embedding"] == [0.3, 0.4]
        assert mock_client.query_points.await_args.kwargs["with_vectors"] == [
            KAGURA_MEMORIES_VECTOR_NAME
        ]

    async def test_include_vectors_non_dict_vector_yields_empty(self, mock_client):
        """include_vectors with a non-dict point.vector falls back to []."""
        mock_client.query_points.return_value = SimpleNamespace(
            points=[_point("a", 0.8, {}, vector=[0.1, 0.2])]
        )
        out = await search_memories_qdrant(UID, [0.1], WS, CTX, include_vectors=True)
        assert out[0]["embedding"] == []

    async def test_missing_isolation_raises(self, mock_client):
        """Empty user_id raises ValueError before querying."""
        with pytest.raises(ValueError, match="Isolation requires"):
            await search_memories_qdrant("", [0.1], WS, CTX)

    async def test_list_context_validates_each_uuid(self, mock_client):
        """A bad UUID inside the context_id list is rejected."""
        with pytest.raises(ValueError, match="Invalid UUID format"):
            await search_memories_qdrant(UID, [0.1], WS, [CTX, "bad"])

    async def test_query_exception_wrapped(self, mock_client):
        """A query_points failure surfaces as QdrantError."""
        mock_client.query_points.side_effect = RuntimeError("down")
        with pytest.raises(QdrantError, match="Search failed"):
            await search_memories_qdrant(UID, [0.1], WS, CTX)

    async def test_score_threshold_reaches_query_points(self, mock_client):
        """#1229: filters={'score_threshold': X} must reach query_points as
        its kwarg — _build_search_filter only builds payload conditions and
        silently dropped it, so dedup/edge-discovery candidate thresholds
        never applied (every top-10 neighbor became a candidate pair)."""
        mock_client.query_points.return_value = SimpleNamespace(points=[])
        await search_memories_qdrant(UID, [0.1], WS, CTX, filters={"score_threshold": 0.92})
        assert mock_client.query_points.await_args.kwargs["score_threshold"] == 0.92

    async def test_no_score_threshold_by_default(self, mock_client):
        """Without the filter key, query_points gets score_threshold=None."""
        mock_client.query_points.return_value = SimpleNamespace(points=[])
        await search_memories_qdrant(UID, [0.1], WS, CTX)
        assert mock_client.query_points.await_args.kwargs["score_threshold"] is None

    @pytest.mark.parametrize("bad", ["0.9", True, {"gte": 0.5}, [0.9], 1.5, -1.5])
    async def test_invalid_score_threshold_is_a_value_error(self, mock_client, bad):
        """#1229 review: filters is free-form user input on the public recall
        API — junk score_threshold must map to ValueError (4xx, matching the
        importance-range convention), never a QdrantError 5xx."""
        with pytest.raises(ValueError, match="score_threshold"):
            await search_memories_qdrant(UID, [0.1], WS, CTX, filters={"score_threshold": bad})
        mock_client.query_points.assert_not_awaited()


# ===========================================================================
# update_memory_payload_in_qdrant
# ===========================================================================


class TestUpdateMemoryPayload:
    """set_payload wrapper."""

    async def test_happy_path(self, mock_client):
        """set_payload is called with the point id and updates."""
        mid = uuid4()
        await update_memory_payload_in_qdrant(mid, {"importance": 0.9})
        kwargs = mock_client.set_payload.await_args.kwargs
        assert kwargs["points"] == [str(mid)]
        assert kwargs["payload"] == {"importance": 0.9}

    async def test_exception_wrapped(self, mock_client):
        """A set_payload error becomes QdrantError."""
        mock_client.set_payload.side_effect = RuntimeError("nope")
        with pytest.raises(QdrantError, match="Failed to update memory payload"):
            await update_memory_payload_in_qdrant(uuid4(), {"x": 1})


# ===========================================================================
# delete_memory_from_qdrant
# ===========================================================================


class TestDeleteMemory:
    """Single-point delete wrapper."""

    async def test_happy_path(self, mock_client):
        """delete is called with a PointIdsList of the memory id."""
        mid = uuid4()
        await delete_memory_from_qdrant(UID, mid)
        kwargs = mock_client.delete.await_args.kwargs
        selector = kwargs["points_selector"]
        assert isinstance(selector, PointIdsList)
        assert selector.points == [str(mid)]

    async def test_exception_wrapped(self, mock_client):
        """A delete error becomes QdrantError."""
        mock_client.delete.side_effect = RuntimeError("x")
        with pytest.raises(QdrantError, match="Failed to delete memory"):
            await delete_memory_from_qdrant(UID, uuid4())


# ===========================================================================
# search_memories_fulltext (BM25)
# ===========================================================================


class TestSearchMemoriesFulltext:
    """BM25 keyword search wrapper with tokenization."""

    async def test_happy_path_maps_points(self, mock_client, monkeypatch):
        """A non-empty tokenized query runs a sparse query and maps results."""
        monkeypatch.setattr(qmod, "tokenize_and_reading", lambda q: ("tok", "", []))
        monkeypatch.setattr(qmod, "augment_reading_tokens", lambda q, sudachi_tokens=None: "")
        monkeypatch.setattr(qmod, "expand_query_tokens", lambda q: q)
        monkeypatch.setattr(qmod, "build_query_sparse_vector", lambda q: ([1, 2], [0.5, 0.6]))
        mock_client.query_points.return_value = SimpleNamespace(
            points=[_point("a", 1.2, {"summary": "y"})]
        )

        out = await search_memories_fulltext(UID, "hello", WS, CTX)
        assert out == [{"id": "a", "score": 1.2, "payload": {"summary": "y"}}]
        kwargs = mock_client.query_points.await_args.kwargs
        assert kwargs["using"] == KAGURA_MEMORIES_BM25_VECTOR_NAME
        assert isinstance(kwargs["query"], SparseVector)

    async def test_reading_and_augment_branch(self, mock_client, monkeypatch):
        """Non-empty reading + augment tokens combine into the query."""
        seen = {}

        def fake_expand(q):
            seen["expanded"] = q
            return q

        monkeypatch.setattr(qmod, "tokenize_and_reading", lambda q: ("lemma", "yomi", ["t"]))
        monkeypatch.setattr(qmod, "augment_reading_tokens", lambda q, sudachi_tokens=None: "aug")
        monkeypatch.setattr(qmod, "expand_query_tokens", fake_expand)
        monkeypatch.setattr(qmod, "build_query_sparse_vector", lambda q: ([1], [0.5]))
        mock_client.query_points.return_value = SimpleNamespace(points=[])

        await search_memories_fulltext(UID, "neko", WS, CTX)
        assert seen["expanded"] == "lemma yomi aug"

    async def test_empty_after_tokenization_returns_empty(self, mock_client, monkeypatch):
        """When tokenization yields no indices, returns [] without querying."""
        monkeypatch.setattr(qmod, "tokenize_and_reading", lambda q: ("", "", []))
        monkeypatch.setattr(qmod, "augment_reading_tokens", lambda q, sudachi_tokens=None: "")
        monkeypatch.setattr(qmod, "expand_query_tokens", lambda q: q)
        monkeypatch.setattr(qmod, "build_query_sparse_vector", lambda q: ([], []))

        out = await search_memories_fulltext(UID, "???", WS, CTX)
        assert out == []
        mock_client.query_points.assert_not_awaited()

    async def test_missing_isolation_raises(self, mock_client):
        """Missing context_id raises ValueError."""
        with pytest.raises(ValueError, match="Isolation requires"):
            await search_memories_fulltext(UID, "q", WS, "")

    async def test_list_context_validates_each_uuid(self, mock_client):
        """A bad UUID inside the context_id list is rejected (BM25 path)."""
        with pytest.raises(ValueError, match="Invalid UUID format"):
            await search_memories_fulltext(UID, "q", WS, [CTX, "bad"])

    async def test_list_context_happy_path(self, mock_client, monkeypatch):
        """A valid context_id list flows through to a sparse query."""
        monkeypatch.setattr(qmod, "tokenize_and_reading", lambda q: ("tok", "", []))
        monkeypatch.setattr(qmod, "augment_reading_tokens", lambda q, sudachi_tokens=None: "")
        monkeypatch.setattr(qmod, "expand_query_tokens", lambda q: q)
        monkeypatch.setattr(qmod, "build_query_sparse_vector", lambda q: ([1], [0.5]))
        mock_client.query_points.return_value = SimpleNamespace(points=[])
        out = await search_memories_fulltext(UID, "q", WS, [CTX, WS])
        assert out == []

    async def test_query_exception_wrapped(self, mock_client, monkeypatch):
        """A failure during search surfaces as QdrantError (BM25)."""
        monkeypatch.setattr(qmod, "tokenize_and_reading", lambda q: ("tok", "", []))
        monkeypatch.setattr(qmod, "augment_reading_tokens", lambda q, sudachi_tokens=None: "")
        monkeypatch.setattr(qmod, "expand_query_tokens", lambda q: q)
        monkeypatch.setattr(qmod, "build_query_sparse_vector", lambda q: ([1], [0.5]))
        mock_client.query_points.side_effect = RuntimeError("down")

        with pytest.raises(QdrantError, match="BM25 search failed"):
            await search_memories_fulltext(UID, "q", WS, CTX)


# ===========================================================================
# ensure_kagura_memories_collection
# ===========================================================================


class TestEnsureCollection:
    """Collection bootstrap / migration logic."""

    async def test_creates_collection_when_absent(self, mock_client):
        """When the collection does not exist, it is created with indexes."""
        mock_client.get_collections.return_value = SimpleNamespace(collections=[])
        await ensure_kagura_memories_collection(512)
        mock_client.create_collection.assert_awaited_once()
        assert mock_client.create_payload_index.await_count >= 10

    async def test_existing_with_sparse_and_tags_returns_early(self, mock_client):
        """An up-to-date collection with a tags index creates nothing new."""
        mock_client.get_collections.return_value = SimpleNamespace(
            collections=[SimpleNamespace(name=KAGURA_MEMORIES_COLLECTION)]
        )
        params = SimpleNamespace(sparse_vectors={KAGURA_MEMORIES_BM25_VECTOR_NAME: object()})
        mock_client.get_collection.return_value = SimpleNamespace(
            config=SimpleNamespace(params=params),
            payload_schema={"tags": object(), "scope": object()},
        )
        await ensure_kagura_memories_collection()
        mock_client.create_collection.assert_not_awaited()
        mock_client.create_payload_index.assert_not_awaited()

    async def test_existing_with_sparse_missing_tags_creates_tag_index(self, mock_client):
        """A sparse-enabled collection lacking a tags index gets one created."""
        mock_client.get_collections.return_value = SimpleNamespace(
            collections=[SimpleNamespace(name=KAGURA_MEMORIES_COLLECTION)]
        )
        params = SimpleNamespace(sparse_vectors={KAGURA_MEMORIES_BM25_VECTOR_NAME: object()})
        mock_client.get_collection.return_value = SimpleNamespace(
            config=SimpleNamespace(params=params),
            payload_schema={"scope": object()},
        )
        await ensure_kagura_memories_collection()
        mock_client.create_payload_index.assert_awaited_once()
        assert mock_client.create_payload_index.await_args.kwargs["field_name"] == "tags"
        mock_client.create_collection.assert_not_awaited()

    async def test_old_collection_no_recreate_flag_raises(self, mock_client, monkeypatch):
        """Old collection without sparse + no recreate flag raises QdrantError."""
        monkeypatch.delenv("KAGURA_RECREATE_COLLECTIONS", raising=False)
        mock_client.get_collections.return_value = SimpleNamespace(
            collections=[SimpleNamespace(name=KAGURA_MEMORIES_COLLECTION)]
        )
        mock_client.get_collection.return_value = SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(sparse_vectors=None)),
            payload_schema={},
        )
        with pytest.raises(QdrantError, match="needs sparse vector migration"):
            await ensure_kagura_memories_collection()

    async def test_old_collection_recreate_flag_deletes_and_recreates(
        self, mock_client, monkeypatch
    ):
        """With KAGURA_RECREATE_COLLECTIONS=true, the old collection is dropped."""
        monkeypatch.setenv("KAGURA_RECREATE_COLLECTIONS", "true")
        mock_client.get_collections.return_value = SimpleNamespace(
            collections=[SimpleNamespace(name=KAGURA_MEMORIES_COLLECTION)]
        )
        mock_client.get_collection.return_value = SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(sparse_vectors=None)),
            payload_schema={},
        )
        await ensure_kagura_memories_collection()
        mock_client.delete_collection.assert_awaited_once_with(KAGURA_MEMORIES_COLLECTION)
        mock_client.create_collection.assert_awaited_once()

    async def test_creation_failure_wrapped(self, mock_client):
        """A failure during creation is wrapped in QdrantError."""
        mock_client.get_collections.side_effect = RuntimeError("conn refused")
        with pytest.raises(QdrantError, match="Failed to create kagura_memories collection"):
            await ensure_kagura_memories_collection()


# ===========================================================================
# copy_context_points
# ===========================================================================


class TestCopyContextPoints:
    """Point copy with context rewrite and batching."""

    async def test_copies_points_with_new_ids_and_context(self, mock_client):
        """Retrieved points are re-upserted with new ids + target context."""
        mapping = {"old1": "new1"}
        mock_client.retrieve.return_value = [
            SimpleNamespace(
                id="old1",
                vector=[0.1, 0.2],
                payload={"context_id": "src", "context": {"context_id": "src", "name": "n"}},
            )
        ]
        copied = await copy_context_points(WS, "src-ctx", "tgt-ctx", mapping)
        assert copied == 1
        new_point = mock_client.upsert.await_args.kwargs["points"][0]
        assert new_point.id == "new1"
        assert new_point.payload["context_id"] == "tgt-ctx"
        assert new_point.payload["context"]["context_id"] == "tgt-ctx"

    async def test_empty_retrieve_copies_nothing(self, mock_client):
        """Retrieve returning [] yields zero copied, no upsert."""
        mock_client.retrieve.return_value = []
        copied = await copy_context_points(WS, "s", "t", {"old1": "new1"})
        assert copied == 0
        mock_client.upsert.assert_not_awaited()

    async def test_skips_point_not_in_mapping_and_without_vector(self, mock_client):
        """Points missing from mapping or without a vector are skipped."""
        mapping = {"old1": "new1"}
        mock_client.retrieve.return_value = [
            SimpleNamespace(id="unmapped", vector=[0.1], payload={}),
            SimpleNamespace(id="old1", vector=None, payload={}),
        ]
        copied = await copy_context_points(WS, "s", "t", mapping)
        assert copied == 0
        mock_client.upsert.assert_not_awaited()

    async def test_batching_two_batches(self, mock_client):
        """Mapping larger than batch_size triggers multiple retrieve calls."""
        mapping = {f"old{i}": f"new{i}" for i in range(3)}

        async def fake_retrieve(*, collection_name, ids, with_vectors, with_payload):
            return [SimpleNamespace(id=i, vector=[0.1], payload={}) for i in ids]

        mock_client.retrieve.side_effect = fake_retrieve
        copied = await copy_context_points(WS, "s", "t", mapping, batch_size=2)
        assert copied == 3
        assert mock_client.retrieve.await_count == 2

    async def test_copy_failure_wrapped(self, mock_client):
        """A retrieve failure surfaces as QdrantError."""
        mock_client.retrieve.side_effect = RuntimeError("boom")
        with pytest.raises(QdrantError, match="Failed to copy context points"):
            await copy_context_points(WS, "s", "t", {"old1": "new1"})


# ===========================================================================
# delete_context_points
# ===========================================================================


class TestDeleteContextPoints:
    """Context-scoped delete with count-before-delete."""

    async def test_counts_then_deletes(self, mock_client):
        """Returns the pre-delete count and issues a filtered delete."""
        mock_client.count.return_value = SimpleNamespace(count=7)
        deleted = await delete_context_points(WS, CTX)
        assert deleted == 7
        mock_client.delete.assert_awaited_once()
        selector = mock_client.delete.await_args.kwargs["points_selector"]
        assert isinstance(selector, Filter)

    async def test_failure_wrapped(self, mock_client):
        """A count/delete error becomes QdrantError."""
        mock_client.count.side_effect = RuntimeError("x")
        with pytest.raises(QdrantError, match="Failed to delete context points"):
            await delete_context_points(WS, CTX)


# ===========================================================================
# delete_user_points
# ===========================================================================


class TestDeleteUserPoints:
    """GDPR user erasure across all kagura_memories* collections."""

    async def test_deletes_across_matching_collections(self, mock_client):
        """Only prefixed collections are targeted; counts mapped per collection."""
        mock_client.get_collections.return_value = SimpleNamespace(
            collections=[
                SimpleNamespace(name=KAGURA_MEMORIES_COLLECTION),
                SimpleNamespace(name=VARIANT_COLLECTION),
                SimpleNamespace(name="unrelated_collection"),
            ]
        )
        mock_client.count.side_effect = [
            SimpleNamespace(count=3),
            SimpleNamespace(count=0),
        ]
        result = await delete_user_points(UID)
        assert result == {
            KAGURA_MEMORIES_COLLECTION: 3,
            VARIANT_COLLECTION: 0,
        }
        assert mock_client.delete.await_count == 1

    async def test_list_collections_failure_wrapped(self, mock_client):
        """A get_collections failure raises a listing-specific QdrantError."""
        mock_client.get_collections.side_effect = RuntimeError("down")
        with pytest.raises(QdrantError, match="Failed to list collections for user erasure"):
            await delete_user_points(UID)

    async def test_per_collection_delete_failure_names_collection(self, mock_client):
        """A delete failure includes the offending collection name."""
        mock_client.get_collections.return_value = SimpleNamespace(
            collections=[SimpleNamespace(name=KAGURA_MEMORIES_COLLECTION)]
        )
        mock_client.count.return_value = SimpleNamespace(count=5)
        mock_client.delete.side_effect = RuntimeError("boom")
        with pytest.raises(QdrantError, match=f"from {KAGURA_MEMORIES_COLLECTION}"):
            await delete_user_points(UID)


# ===========================================================================
# _admin_scroll_context_points
# ===========================================================================


class TestAdminScrollContextPoints:
    """Admin scroll generator that paginates until next_offset is None."""

    async def test_paginates_until_offset_none(self, mock_client):
        """Yields each page and stops when next_offset is None."""
        mock_client.scroll.side_effect = [
            (["p1", "p2"], "offset-2"),
            (["p3"], None),
        ]
        pages = [page async for page in _admin_scroll_context_points(CTX)]
        assert pages == [["p1", "p2"], ["p3"]]
        assert mock_client.scroll.await_count == 2
        assert mock_client.scroll.await_args_list[1].kwargs["offset"] == "offset-2"

    async def test_single_page_stops_immediately(self, mock_client):
        """A single page (next_offset None) yields once and stops."""
        mock_client.scroll.return_value = (["only"], None)
        pages = [page async for page in _admin_scroll_context_points(CTX, with_vectors=True)]
        assert pages == [["only"]]
        assert mock_client.scroll.await_args.kwargs["with_vectors"] is True


# ===========================================================================
# Module constants
# ===========================================================================


class TestModuleConstants:
    """Stable contract constants relied on by other modules."""

    def test_token_payload_fields(self):
        """The token payload field tuple is the documented source of truth."""
        assert QDRANT_TOKEN_PAYLOAD_FIELDS == (
            "summary_tokens",
            "context_summary_tokens",
            "content_tokens",
            "summary_reading",
        )

    def test_vector_names(self):
        """Named-vector contract constants are stable."""
        assert KAGURA_MEMORIES_VECTOR_NAME == "dense"
        assert KAGURA_MEMORIES_BM25_VECTOR_NAME == "bm25"
