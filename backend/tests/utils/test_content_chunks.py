"""Unit tests for the #2015 content-chunk helpers (pure functions)."""

from uuid import UUID, uuid4

from utils.content_chunks import (
    CHUNK_OF_FIELD,
    CONTENT_CHUNK_CHARS,
    CONTENT_CHUNK_MAX_PER_MEMORY,
    CONTENT_CHUNK_MIN_CONTENT_CHARS,
    CONTENT_CHUNK_OVERLAP_CHARS,
    RERANK_CHUNK_TEXT_MAX_CHARS,
    build_chunk_payload,
    chunk_point_id,
    collapse_chunk_hits,
    rerank_document,
    split_content,
)


class TestSplitContent:
    def test_short_content_has_no_chunks(self):
        assert split_content("x" * (CONTENT_CHUNK_MIN_CONTENT_CHARS - 1)) == []
        assert split_content("") == []
        assert split_content(None) == []

    def test_chunks_are_fixed_size_with_overlap(self):
        text = "".join(chr(ord("a") + i % 26) for i in range(CONTENT_CHUNK_CHARS * 3))
        chunks = split_content(text)
        step = CONTENT_CHUNK_CHARS - CONTENT_CHUNK_OVERLAP_CHARS
        assert chunks[0] == text[:CONTENT_CHUNK_CHARS]
        assert chunks[1] == text[step : step + CONTENT_CHUNK_CHARS]
        assert all(len(c) <= CONTENT_CHUNK_CHARS for c in chunks)
        # The tail is covered.
        assert chunks[-1].endswith(text[-10:])

    def test_chunk_count_is_capped(self):
        text = "y" * (CONTENT_CHUNK_CHARS * (CONTENT_CHUNK_MAX_PER_MEMORY + 10))
        assert len(split_content(text)) == CONTENT_CHUNK_MAX_PER_MEMORY

    def test_no_extra_chunk_once_the_tail_is_covered(self):
        step = CONTENT_CHUNK_CHARS - CONTENT_CHUNK_OVERLAP_CHARS
        text = "z" * (step + CONTENT_CHUNK_CHARS)
        assert len(split_content(text)) == 2


class TestChunkPointId:
    def test_deterministic_and_distinct(self):
        mid = uuid4()
        assert chunk_point_id(mid, 0) == chunk_point_id(mid, 0)
        assert chunk_point_id(mid, 0) != chunk_point_id(mid, 1)
        assert chunk_point_id(mid, 0) != chunk_point_id(uuid4(), 0)
        assert chunk_point_id(mid, 0) != str(mid)
        UUID(chunk_point_id(mid, 3))


class TestBuildChunkPayload:
    def test_keeps_filter_fields_drops_tokens_adds_chunk_fields(self):
        mid = uuid4()
        base = {
            "user_id": "u",
            "summary": "s",
            "context_summary": "c",
            "summary_tokens": "t",
            "context_summary_tokens": "t",
            "content_tokens": "t",
            "summary_reading": "r",
            "type": "note",
            "importance": 0.5,
            "tags": ["a"],
            "scope": "context",
            "client": "x",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "location": {"lat": 1.0, "lon": 2.0},
        }
        payload = build_chunk_payload(base, memory_id=mid, chunk_index=2, chunk_text="body")
        for key in (
            "summary_tokens",
            "context_summary_tokens",
            "content_tokens",
            "summary_reading",
        ):
            assert key not in payload
        for key in ("user_id", "summary", "context_summary", "type", "importance", "tags", "scope"):
            assert payload[key] == base[key]
        assert payload["location"] == base["location"]
        assert payload[CHUNK_OF_FIELD] == str(mid)
        assert payload["chunk_index"] == 2
        assert payload["chunk_text"] == "body"
        # The base payload is not mutated.
        assert "summary_tokens" in base and CHUNK_OF_FIELD not in base


def _hit(pid, score, chunk_of=None, chunk_text=None, chunk_index=None, embedding=None):
    payload = {"summary": "s"}
    if chunk_of is not None:
        payload[CHUNK_OF_FIELD] = chunk_of
        payload["chunk_text"] = chunk_text
        payload["chunk_index"] = chunk_index
    return {"id": pid, "score": score, "payload": payload, "embedding": embedding or []}


class TestCollapseChunkHits:
    def test_no_chunks_is_identity(self):
        hits = [_hit("a", 0.9), _hit("b", 0.5)]
        assert collapse_chunk_hits(hits) == hits

    def test_max_score_wins_and_summary_point_payload_and_embedding_kept(self):
        hits = [
            _hit("c1", 0.95, chunk_of="m1", chunk_text="late passage", chunk_index=3),
            _hit("m1", 0.40, embedding=[0.1, 0.2]),
            _hit("m2", 0.60),
        ]
        out = collapse_chunk_hits(hits)
        assert [h["id"] for h in out] == ["m1", "m2"]
        assert out[0]["score"] == 0.95
        assert out[0]["embedding"] == [0.1, 0.2]
        assert CHUNK_OF_FIELD not in out[0]["payload"]
        assert out[0]["matched_chunk_text"] == "late passage"
        assert out[0]["matched_chunk_index"] == 3
        assert out[1]["matched_chunk_text"] is None

    def test_summary_point_winning_sets_no_chunk_text(self):
        hits = [
            _hit("m1", 0.9),
            _hit("c1", 0.5, chunk_of="m1", chunk_text="x", chunk_index=0),
        ]
        out = collapse_chunk_hits(hits)
        assert len(out) == 1
        assert out[0]["matched_chunk_text"] is None
        assert out[0]["score"] == 0.9

    def test_chunk_only_group_uses_chunk_payload_and_memory_id(self):
        hits = [_hit("c1", 0.7, chunk_of="m9", chunk_text="t", chunk_index=1)]
        out = collapse_chunk_hits(hits)
        assert out[0]["id"] == "m9"
        assert out[0]["embedding"] == []
        assert out[0]["payload"]["summary"] == "s"
        assert CHUNK_OF_FIELD not in out[0]["payload"]
        assert "chunk_text" not in out[0]["payload"]

    def test_output_sorted_by_score(self):
        hits = [
            _hit("m1", 0.2),
            _hit("c2", 0.8, chunk_of="m2", chunk_text="t", chunk_index=0),
            _hit("m3", 0.5),
        ]
        assert [h["id"] for h in collapse_chunk_hits(hits)] == ["m2", "m3", "m1"]


class TestRerankDocument:
    def test_summary_and_context_summary(self):
        doc = rerank_document({"payload": {"summary": "S", "context_summary": "C"}})
        assert doc == "S C"

    def test_missing_context_summary(self):
        assert rerank_document({"payload": {"summary": "S"}}) == "S "

    def test_chunk_text_appended_after_summary_and_truncated(self):
        long = "q" * (RERANK_CHUNK_TEXT_MAX_CHARS + 50)
        doc = rerank_document(
            {"payload": {"summary": "S", "context_summary": "C"}, "matched_chunk_text": long}
        )
        assert doc.startswith("S C\n")
        assert doc.endswith("q" * RERANK_CHUNK_TEXT_MAX_CHARS)
        assert len(doc) == len("S C\n") + RERANK_CHUNK_TEXT_MAX_CHARS
