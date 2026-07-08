"""Unit tests for the deterministic query-intent classifier (#1212).

Pins the acceptance contract: pure function, deterministic, sub-millisecond,
and the rule table from the issue — exact-ID / quoted-literal / code-symbol
→ keyword, hiragana-dominant → keyword (Sudachi lane), keyword signal plus
substantial natural language → hybrid, everything else → semantic.
"""

import time

from services.query_router import (
    LANE_HYBRID,
    LANE_KEYWORD,
    LANE_SEMANTIC,
    QueryRoute,
    classify_query,
)


class TestKeywordSignals:
    def test_uuid_routes_keyword(self) -> None:
        route = classify_query("550e8400-e29b-41d4-a716-446655440000")
        assert route.lane == LANE_KEYWORD
        assert "exact_id" in route.reasons

    def test_issue_reference_routes_keyword(self) -> None:
        assert classify_query("#1212").lane == LANE_KEYWORD

    def test_version_string_routes_keyword(self) -> None:
        assert classify_query("v0.45.0 changelog").lane == LANE_KEYWORD

    def test_error_code_routes_keyword(self) -> None:
        route = classify_query("HEALTH-001")
        assert route.lane == LANE_KEYWORD
        assert "exact_id" in route.reasons

    def test_commit_sha_routes_keyword(self) -> None:
        assert classify_query("2510682f").lane == LANE_KEYWORD

    def test_quoted_literal_routes_keyword(self) -> None:
        route = classify_query('"ConnectionResetError" retry')
        assert route.lane == LANE_KEYWORD
        assert "quoted_literal" in route.reasons

    def test_snake_case_symbol_routes_keyword(self) -> None:
        route = classify_query("resolve_context_routing")
        assert route.lane == LANE_KEYWORD
        assert "code_symbol" in route.reasons

    def test_camel_case_symbol_routes_keyword(self) -> None:
        assert classify_query("MemoryHealthService grading").lane == LANE_KEYWORD

    def test_dotted_path_routes_keyword(self) -> None:
        assert classify_query("services.memory_service.recall").lane == LANE_KEYWORD


class TestMixedSignals:
    def test_symbol_with_natural_question_routes_hybrid(self) -> None:
        route = classify_query(
            "how does resolve_context_routing decide the collection name for shared contexts"
        )
        assert route.lane == LANE_HYBRID
        assert "mixed_natural_language" in route.reasons

    def test_quoted_error_with_long_description_routes_hybrid(self) -> None:
        route = classify_query(
            'what causes "UndefinedColumnError" when the local database was stamped without migrations'
        )
        assert route.lane == LANE_HYBRID


class TestHiraganaLane:
    def test_hiragana_dominant_routes_keyword(self) -> None:
        route = classify_query("きのうのぶんをさがして")
        assert route.lane == LANE_KEYWORD
        assert route.reasons == ("hiragana_dominant",)

    def test_kanji_mixed_japanese_routes_semantic(self) -> None:
        # 認証エラーの解決方法: hiragana share is 1/10 — kanji-carried content
        # is embedding-friendly, so this stays on the semantic lane.
        assert classify_query("認証エラーの解決方法").lane == LANE_SEMANTIC

    def test_short_hiragana_below_min_chars_routes_semantic(self) -> None:
        assert classify_query("これ").lane == LANE_SEMANTIC


class TestApostropheSafety:
    """Inner-review regression: apostrophes in ordinary English must never
    read as an opening single-quote literal (semantic-lane hijack)."""

    def test_contractions_stay_semantic(self) -> None:
        route = classify_query("what's the user's role in this workspace")
        assert route.lane == LANE_SEMANTIC
        assert route.features["quoted_literals"] == 0

    def test_negated_contraction_stays_semantic(self) -> None:
        assert (
            classify_query("why doesn't the reranker trigger when there's no BYOK key").lane
            == LANE_SEMANTIC
        )

    def test_delimited_single_quote_literal_still_routes_keyword(self) -> None:
        route = classify_query("'exact phrase' lookup")
        assert route.lane == LANE_KEYWORD
        assert "quoted_literal" in route.reasons


class TestRemainderStripping:
    """Inner-review regression: longest-span-first stripping — snake_case
    substrings of dotted paths must not leak fragments as natural tokens."""

    def test_pure_dotted_path_query_routes_keyword(self) -> None:
        route = classify_query("check services.memory_service.py and services.context_service.py")
        assert route.lane == LANE_KEYWORD
        assert route.features["natural_tokens"] < 4


class TestSemanticDefault:
    def test_natural_english_question_routes_semantic(self) -> None:
        route = classify_query("what were the design decisions for sleep maintenance")
        assert route.lane == LANE_SEMANTIC
        assert route.reasons == ("default_semantic",)

    def test_empty_query_routes_semantic(self) -> None:
        route = classify_query("   ")
        assert route.lane == LANE_SEMANTIC
        assert route.features["script_chars"] == 0


class TestContract:
    def test_deterministic(self) -> None:
        q = 'why does "reinforce_rerank" boost v0.44.0 recall ordering'
        assert classify_query(q) == classify_query(q)

    def test_features_never_contain_query_text(self) -> None:
        """Telemetry safety: features are numeric-only (no query content)."""
        route = classify_query("secret PII-like query about JWT_SECRET rotation")
        assert all(isinstance(v, (int, float)) for v in route.features.values())

    def test_returns_frozen_route(self) -> None:
        route = classify_query("anything")
        assert isinstance(route, QueryRoute)

    def test_sub_millisecond_average(self) -> None:
        """Acceptance: sub-ms per call. Pinned loosely (0.5 ms avg over a
        long adversarial query) so CI jitter cannot flake it."""
        q = (
            'how does "hybrid_search" in services.search_service merge BM25 and semantic '
            "scores for v0.45.0 with fetch_factor 3 and #1212 routing まぜこぜのくえり "
        ) * 3
        n = 2000
        start = time.perf_counter()
        for _ in range(n):
            classify_query(q)
        avg = (time.perf_counter() - start) / n
        assert avg < 0.0005, f"classifier too slow: {avg * 1000:.3f} ms/call"
