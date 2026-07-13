"""Tests for the LanceDB SQL filter builder + FTS text builder.

Pure-Python — does NOT require lancedb. Exercises the security-critical filter
string construction (isolation, escaping, injection resistance) and the
weighted FTS text assembly, independent of any LanceDB I/O.
"""

import pytest

from db.lance_store import (
    _build_search_text,
    _sql_str,
    build_lance_filter,
)

WS = "11111111-1111-1111-1111-111111111111"
CTX = "22222222-2222-2222-2222-222222222222"
CTX2 = "33333333-3333-3333-3333-333333333333"
# user_id is the OAuth2 `sub`, NOT a UUID (e.g. "google-oauth2|123").
USER = "google-oauth2|123"


# -- _sql_str escaping -------------------------------------------------------
def test_sql_str_quotes_value():
    assert _sql_str("abc") == "'abc'"


def test_sql_str_escapes_single_quotes():
    assert _sql_str("a'b") == "'a''b'"
    assert _sql_str("' OR 1=1 --") == "''' OR 1=1 --'"


# -- isolation core ----------------------------------------------------------
def test_basic_isolation():
    where = build_lance_filter(WS, CTX, USER)
    assert where == (f"workspace_id = '{WS}' AND context_id = '{CTX}' AND user_id = '{USER}'")


def test_shared_context_omits_user():
    where = build_lance_filter(WS, CTX, USER, is_shared_context=True)
    assert "user_id" not in where
    assert where == f"workspace_id = '{WS}' AND context_id = '{CTX}'"


def test_context_list_uses_in():
    where = build_lance_filter(WS, [CTX, CTX2], USER)
    assert f"context_id IN ('{CTX}', '{CTX2}')" in where


def test_user_id_with_quote_is_escaped():
    where = build_lance_filter(WS, CTX, "a'b")
    assert "user_id = 'a''b'" in where


# -- isolation validation ----------------------------------------------------
def test_invalid_workspace_id_raises():
    with pytest.raises(ValueError):
        build_lance_filter("not-a-uuid", CTX, USER)


def test_invalid_context_id_raises():
    with pytest.raises(ValueError):
        build_lance_filter(WS, "not-a-uuid", USER)


def test_invalid_context_id_in_list_raises():
    with pytest.raises(ValueError):
        build_lance_filter(WS, [CTX, "bad"], USER)


# -- metadata filters --------------------------------------------------------
def test_scope_and_type():
    where = build_lance_filter(WS, CTX, USER, filters={"scope": "persistent", "type": "code"})
    assert "scope = 'persistent'" in where
    assert "type = 'code'" in where


def test_importance_range():
    where = build_lance_filter(WS, CTX, USER, filters={"importance": {"gte": 0.5, "lte": 0.9}})
    assert "importance >= 0.5" in where
    assert "importance <= 0.9" in where


def test_importance_gt_lt():
    where = build_lance_filter(WS, CTX, USER, filters={"importance": {"gt": 0.1, "lt": 0.8}})
    assert "importance > 0.1" in where
    assert "importance < 0.8" in where


def test_tags_any_is_or():
    where = build_lance_filter(WS, CTX, USER, filters={"tags": ["x", "y"]})
    assert "(array_has(tags, 'x') OR array_has(tags, 'y'))" in where


def test_tags_all_is_and():
    where = build_lance_filter(WS, CTX, USER, filters={"tags": ["x", "y"], "tags_match": "all"})
    assert "(array_has(tags, 'x') AND array_has(tags, 'y'))" in where


def test_tag_value_is_escaped():
    where = build_lance_filter(WS, CTX, USER, filters={"tags": ["a'b"]})
    assert "array_has(tags, 'a''b')" in where


def test_invalid_tags_match_raises():
    with pytest.raises(ValueError, match="tags_match"):
        build_lance_filter(WS, CTX, USER, filters={"tags": ["x"], "tags_match": "maybe"})


def test_empty_tags_list_ignored():
    where = build_lance_filter(WS, CTX, USER, filters={"tags": []})
    assert "array_has" not in where


def test_date_clauses():
    where = build_lance_filter(
        WS,
        CTX,
        USER,
        filters={"created_after": "2026-01-01T00:00:00Z", "updated_before": "2026-06-01T00:00:00Z"},
    )
    assert "created_at >= '2026-01-01T00:00:00Z'" in where
    assert "updated_at <= '2026-06-01T00:00:00Z'" in where


def test_non_string_date_raises():
    with pytest.raises(ValueError):
        build_lance_filter(WS, CTX, USER, filters={"created_after": 12345})


# -- FTS text builder (field weighting via repetition) -----------------------
def test_search_text_weights_summary_and_context_double():
    payload = {
        "summary_tokens": "alpha",
        "context_summary_tokens": "beta",
        "content_tokens": "gamma",
        "summary_reading": "アルファ",
    }
    text = _build_search_text(payload)
    tokens = text.split()
    assert tokens.count("alpha") == 2  # summary weight 2.0
    assert tokens.count("beta") == 2  # context_summary weight 2.0
    assert tokens.count("gamma") == 1  # content weight 1.0
    assert "アルファ" in tokens  # reading fallback included


def test_search_text_empty_payload():
    assert _build_search_text({}) == ""


# -- importance range consistency (mirrors the Qdrant 4xx validation) --------
def test_importance_inverted_gte_lte_raises():
    with pytest.raises(ValueError, match="gte"):
        build_lance_filter(WS, CTX, USER, filters={"importance": {"gte": 0.9, "lte": 0.1}})


def test_importance_inverted_gt_lt_raises():
    with pytest.raises(ValueError, match="gt"):
        build_lance_filter(WS, CTX, USER, filters={"importance": {"gt": 0.8, "lt": 0.2}})


# -- date normalization: any offset is converted to canonical UTC '...Z' -----
def test_date_offset_normalized_to_utc():
    where = build_lance_filter(
        WS, CTX, USER, filters={"created_after": "2026-06-01T09:00:00+09:00"}
    )
    assert "created_at >= '2026-06-01T00:00:00Z'" in where  # 09:00+09:00 == 00:00Z


def test_date_malformed_raises():
    with pytest.raises(ValueError):
        build_lance_filter(WS, CTX, USER, filters={"created_after": "not-a-date"})


# -- shared context still applies metadata filters (only user_id is dropped) -
def test_shared_context_keeps_metadata_filters():
    where = build_lance_filter(
        WS, CTX, USER, is_shared_context=True, filters={"tags": ["x"], "scope": "persistent"}
    )
    assert "user_id" not in where
    assert "array_has(tags, 'x')" in where
    assert "scope = 'persistent'" in where


# -- #1229: score_threshold parity with the Qdrant path ----------------------
class _StubQuery:
    def __init__(self, rows):
        self._rows = rows

    def metric(self, _name):
        return self

    def where(self, _clause, prefilter=True):
        return self

    def limit(self, _n):
        return self

    def to_list(self):
        return self._rows


class _StubTable:
    def __init__(self, rows):
        self._rows = rows

    def search(self, _vector, vector_column_name="vector"):
        return _StubQuery(self._rows)


@pytest.mark.asyncio
async def test_search_semantic_enforces_score_threshold():
    """#1229: LanceDB has no server-side score_threshold; the same
    filters={'score_threshold': X} contract as the Qdrant path must be
    enforced post-hoc (score = 1 - cosine _distance)."""
    from db.lance_store import LanceVectorStore

    store = LanceVectorStore.__new__(LanceVectorStore)
    rows = [
        {"id": "hi", "_distance": 0.05, "payload_json": "{}"},  # score 0.95
        {"id": "lo", "_distance": 0.45, "payload_json": "{}"},  # score 0.55
    ]
    store._open = lambda _name, dim=0: _StubTable(rows)

    out = await store.search_semantic(USER, [0.1], WS, CTX, filters={"score_threshold": 0.92})
    assert [r["id"] for r in out] == ["hi"]

    out_nofilter = await store.search_semantic(USER, [0.1], WS, CTX)
    assert [r["id"] for r in out_nofilter] == ["hi", "lo"]
