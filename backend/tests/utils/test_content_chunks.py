"""Unit tests for the #2015 content-chunk helpers (pure functions)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from utils.content_chunks import (
    CHUNK_OF_FIELD,
    CONTENT_CHUNK_CHARS,
    CONTENT_CHUNK_MAX_PER_MEMORY,
    CONTENT_CHUNK_MIN_CONTENT_CHARS,
    CONTENT_CHUNK_OVERLAP_CHARS,
    RERANK_CHUNK_TEXT_MAX_CHARS,
    RERANK_EMPTY_DOCUMENT,
    RERANK_RESOURCE_DOC_MAX_CHARS,
    RERANK_RESOURCE_USER_SUMMARY_ROOM,
    RESOURCE_CONTEXT_SUMMARY_MAX_CHARS,
    build_chunk_payload,
    chunk_point_id,
    collapse_chunk_hits,
    rerank_document,
    resource_context_summary,
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

    def test_a_null_context_summary_reads_as_missing(self):
        # The memory point writes the key with None when there is no context summary.
        with_null = rerank_document({"payload": {"summary": "S", "context_summary": None}})
        assert with_null == rerank_document({"payload": {"summary": "S"}})
        assert "None" not in with_null

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

    # #3031: a resource-indexed point (build_resource_point) has no summary.
    def test_resource_payload_uses_its_content_cut_to_the_maximum(self):
        long = "title: " + "r" * (RERANK_RESOURCE_DOC_MAX_CHARS + 100)
        doc = rerank_document({"payload": {"content": long}})
        assert doc == long[:RERANK_RESOURCE_DOC_MAX_CHARS]

    def test_resource_payload_keeps_a_context_summary_the_user_wrote(self):
        # Not the indexer's cut of the content: an edit, or a remember(external_id) row.
        # It goes after the indexed text, which stays first.
        doc = rerank_document({"payload": {"content": "title: Q3 plan", "context_summary": "why"}})
        assert doc == "title: Q3 plan\nwhy"

    def test_resource_payload_drops_the_indexers_own_context_summary(self):
        content = "title: Q3 plan\ndescription: budget"
        doc = rerank_document({"payload": {"content": content, "context_summary": content}})
        assert doc == content

    def test_a_cut_indexer_context_summary_is_not_repeated(self):
        content = "description: " + "d" * 5000
        summary = resource_context_summary(content)
        assert len(summary) == RESOURCE_CONTEXT_SUMMARY_MAX_CHARS
        doc = rerank_document({"payload": {"content": content, "context_summary": summary}})
        assert doc == content[:RERANK_RESOURCE_DOC_MAX_CHARS]

    def test_a_user_context_summary_stays_inside_the_cap(self):
        # A long content and a long user summary share the cap: the summary
        # gets at most half of it, the content the rest.
        payload = {"content": "body: " + "b" * 3000, "context_summary": "u" * 1000}
        doc = rerank_document({"payload": payload})
        assert len(doc) == RERANK_RESOURCE_DOC_MAX_CHARS
        assert doc.startswith("body: b")
        room = RERANK_RESOURCE_USER_SUMMARY_ROOM
        assert doc.endswith("b\n" + "u" * room)

    def test_a_short_content_leaves_the_rest_of_the_cap_to_the_user_summary(self):
        payload = {"content": "title: T", "context_summary": "u" * 2000}
        doc = rerank_document({"payload": payload})
        assert doc == ("title: T\n" + "u" * 2000)[:RERANK_RESOURCE_DOC_MAX_CHARS]

    def test_a_long_user_summary_never_pushes_the_indexed_text_out(self):
        # A remember(external_id) row or an edited row keeps its own context
        # summary, up to 2,000 characters; the non-lead lines must survive it.
        content = "title: Trail shoe\nbrand: B\ndescription: waterproof " + "w" * 400
        payload = {
            "content": content,
            "context_summary": "u" * 2000,
            "metadata": {"title": "Trail shoe"},
        }
        doc = rerank_document({"payload": payload})
        assert len(doc) == RERANK_RESOURCE_DOC_MAX_CHARS
        assert doc.startswith("title: Trail shoe\nbrand: B\ndescription: waterproof ")
        assert "u" * RERANK_RESOURCE_USER_SUMMARY_ROOM in doc

    def test_lead_fields_come_first_from_the_metadata(self):
        content = "answer: long answer\nquestion: How do I return an item?"
        payload = {
            "content": content,
            "metadata": {"answer": "long answer", "question": "How do I return an item?"},
        }
        doc = rerank_document({"payload": payload})
        assert doc == "question: How do I return an item?\nanswer: long answer"

    def test_a_lead_value_that_is_part_of_another_line_is_not_removed(self):
        content = "subtitle: Q3\ntitle: Q3"
        doc = rerank_document({"payload": {"content": content, "metadata": {"title": "Q3"}}})
        assert doc == "title: Q3\nsubtitle: Q3"

    def test_a_multi_line_lead_value_is_moved_whole(self):
        content = "brand: B\ntitle: line one\nline two"
        doc = rerank_document(
            {"payload": {"content": content, "metadata": {"title": "line one\nline two"}}}
        )
        assert doc == "title: line one\nline two\nbrand: B"

    def test_a_lead_line_keeps_the_label_of_the_indexed_text(self):
        # An owner schema labels a line with the field's description.
        content = "Colour: red\nProduct name: Shoe"
        doc = rerank_document({"payload": {"content": content, "metadata": {"title": "Shoe"}}})
        assert doc == "Product name: Shoe\nColour: red"
        assert "title:" not in doc

    def test_a_lead_field_not_in_the_indexed_text_is_not_sent(self):
        # metadata holds every public field, also one with only a facet hint.
        content = "brand: B\ndescription: D"
        payload = {"content": content, "metadata": {"title": "Shoe", "sku": "SKU-1"}}
        doc = rerank_document({"payload": payload})
        assert doc == content
        assert "Shoe" not in doc

    def test_a_lead_value_on_two_differently_labelled_lines_stays_in_place(self):
        content = "Colour: Red\nProduct name: Red\nsize: 9"
        doc = rerank_document({"payload": {"content": content, "metadata": {"title": "Red"}}})
        assert doc == content

    def test_a_numeric_lead_value_moves_its_line(self):
        content = "brand: B\nsku: 12345"
        doc = rerank_document({"payload": {"content": content, "metadata": {"sku": 12345}}})
        assert doc == "sku: 12345\nbrand: B"

    def test_without_metadata_the_content_comes_first(self):
        content = "brand: B\ntitle: T"
        assert rerank_document({"payload": {"content": content}}) == content
        assert rerank_document({"payload": {"content": content, "metadata": None}}) == content

    def test_a_long_title_alone_is_cut_at_the_cap(self):
        title = "t" * 2048
        payload = {"content": f"title: {title}", "metadata": {"title": title}}
        doc = rerank_document({"payload": payload})
        assert doc == f"title: {title}"[:RERANK_RESOURCE_DOC_MAX_CHARS]

    def test_resource_payload_with_a_matched_chunk(self):
        doc = rerank_document(
            {"payload": {"content": "title: Q3 plan"}, "matched_chunk_text": "chunk body"}
        )
        assert doc == "title: Q3 plan\nchunk body"

    def test_a_long_resource_body_leaves_room_for_the_matched_chunk(self):
        content = "title: Trail shoe\ndescription: " + "d" * 2000
        doc = rerank_document(
            {
                "payload": {"content": content, "metadata": {"title": "Trail shoe"}},
                "matched_chunk_text": "MATCHED chunk text",
            }
        )
        assert len(doc) == RERANK_RESOURCE_DOC_MAX_CHARS
        assert doc.startswith("title: Trail shoe\ndescription: ")
        assert doc.endswith("\nMATCHED chunk text")

    def test_a_matched_chunk_at_its_own_cap_still_fits(self):
        chunk = "c" * (RERANK_CHUNK_TEXT_MAX_CHARS + 10)
        doc = rerank_document(
            {"payload": {"content": "title: T\n" + "d" * 2000}, "matched_chunk_text": chunk}
        )
        assert len(doc) <= RERANK_RESOURCE_DOC_MAX_CHARS
        assert doc.endswith(
            chunk[: min(RERANK_CHUNK_TEXT_MAX_CHARS, RERANK_RESOURCE_DOC_MAX_CHARS)]
        )

    def test_empty_summary_falls_back_to_content(self):
        assert rerank_document({"payload": {"summary": "", "content": "body"}}) == "body"

    def test_payload_without_text_gives_a_non_empty_document(self):
        for candidate in (
            {"payload": {}},
            {"payload": None},
            {},
            {"payload": {"summary": None, "content": None, "context_summary": None}},
            {"payload": {"content": "   "}},
        ):
            doc = rerank_document(candidate)
            assert doc == RERANK_EMPTY_DOCUMENT
            assert doc.strip()

    def test_context_summary_alone_is_the_document(self):
        assert rerank_document({"payload": {"context_summary": "why"}}) == "why"


def _indexed_resource_candidate(fields: dict[str, str]) -> dict:
    """A recall candidate as the resource indexer writes it for a sync record."""
    from models.sync_feed_schemas import DEFINITIONS
    from services.resource_indexer import (
        ResourceIndexer,
        build_resource_point,
        resource_document_text,
    )

    projected = ResourceIndexer._project_payload(
        None,  # type: ignore[arg-type]  # the projection reads no instance state
        fields,
        SimpleNamespace(field_definitions=DEFINITIONS),  # type: ignore[arg-type]
    )
    content = resource_document_text(projected, "doc_1")
    context = SimpleNamespace(workspace_id=uuid4(), id=uuid4(), created_by=uuid4())
    point = build_resource_point(
        context=context,  # type: ignore[arg-type]
        resource_id="res_1",
        doc_id="doc_1",
        version=1,
        content=content,
        projected=projected,
        embedding=[0.1] * 8,
        updated_at=datetime(2026, 11, 1, tzinfo=UTC),
        memory_id=uuid4(),
        context_summary=resource_context_summary(content),
    )
    return {"payload": point.payload}


class TestRerankDocumentOfIndexedSyncRecords:
    """The document of a record indexed through the sync feed schema."""

    def test_a_product_starts_with_title_and_keeps_its_sku(self):
        candidate = _indexed_resource_candidate(
            {
                "record_type": "product",
                "title": "Trail shoe",
                "url": "https://example.com/p/1",
                "brand": "b" * 2048,
                "description": "d" * 16_000,
                "sku": "SKU-123",
            }
        )
        # The indexed text is ordered by field name: brand, description, sku, title.
        assert candidate["payload"]["content"].startswith("brand: ")
        doc = rerank_document(candidate)
        assert doc.startswith("title: Trail shoe\nsku: SKU-123\nbrand: b")
        assert len(doc) == RERANK_RESOURCE_DOC_MAX_CHARS
        assert doc.count("title: Trail shoe") == 1

    def test_an_faq_starts_with_its_question(self):
        candidate = _indexed_resource_candidate(
            {
                "record_type": "faq",
                "question": "How do I return an item?",
                "answer": "a" * 16_000,
            }
        )
        assert candidate["payload"]["content"].startswith("answer: ")
        doc = rerank_document(candidate)
        assert doc.startswith("question: How do I return an item?\nanswer: a")
        assert len(doc) == RERANK_RESOURCE_DOC_MAX_CHARS

    def test_a_normalized_indexer_context_summary_is_recognised(self):
        # The point stores the context summary NFKC-normalized; the content is raw.
        candidate = _indexed_resource_candidate(
            {"record_type": "faq", "question": "㈱ ｶﾀｶﾅ?", "answer": "A."}
        )
        assert candidate["payload"]["context_summary"] != candidate["payload"]["content"]
        assert rerank_document(candidate) == "question: ㈱ ｶﾀｶﾅ?\nanswer: A."

    def test_a_short_record_goes_in_once(self):
        candidate = _indexed_resource_candidate(
            {"record_type": "faq", "question": "Q?", "answer": "A."}
        )
        assert rerank_document(candidate) == "question: Q?\nanswer: A."


def test_resource_context_summary_is_the_indexers_cut():
    assert resource_context_summary("short") == "short"
    exact = "x" * RESOURCE_CONTEXT_SUMMARY_MAX_CHARS
    assert resource_context_summary(exact) == exact
    longer = exact + "y"
    assert resource_context_summary(longer) == exact[:-3] + "..."


def test_chunk_payload_keeps_every_field_of_the_real_summary_point():
    """#2015: built from build_memory_point's own payload, a chunk keeps every
    filterable and isolation-relevant field (user_id here; workspace_id and
    context_id are added by the upsert, as for the summary point)."""
    from models.memory import Memory
    from services.memory_service import build_memory_point
    from utils.datetime import utcnow

    memory = Memory(
        id=uuid4(),
        user_id="owner-1",
        workspace_id=uuid4(),
        context_id=uuid4(),
        summary="a summary",
        context_summary="why",
        content="body",
        type="note",
        importance=0.7,
        tags=["a"],
        scope="context",
        client="pytest",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    base, _, _ = build_memory_point(memory)
    payload = build_chunk_payload(base, memory_id=memory.id, chunk_index=0, chunk_text="t")

    token_fields = {"summary_tokens", "context_summary_tokens", "content_tokens", "summary_reading"}
    for key, value in base.items():
        if key in token_fields:
            assert key not in payload
        else:
            assert payload[key] == value, key
    assert payload["user_id"] == "owner-1"


def test_expected_chunk_count_matches_split_content():
    """#2015 PR 2a: the migration's verify counts chunks from char_length only."""
    from utils.content_chunks import expected_chunk_count

    for length in [*range(0, 3000, 1), *range(3000, 40000, 37)]:
        assert expected_chunk_count(length) == len(split_content("x" * length)), length


class TestContentHash:
    """#2015 PR 2b: the ``content_hash`` chunk payload field."""

    def test_nfc_normalized_and_stable(self):
        from utils.content_chunks import content_hash

        decomposed = "Café"
        assert content_hash(decomposed) == content_hash("Café")
        assert content_hash("a") != content_hash("b")
        assert content_hash(None) == content_hash("")
        assert len(content_hash("a")) == 16

    def test_written_when_given_and_dropped_on_a_collapsed_chunk_hit(self):
        payload = build_chunk_payload(
            {"summary": "s"}, memory_id=uuid4(), chunk_index=0, chunk_text="t", content_hash="h"
        )
        assert payload["content_hash"] == "h"
        assert "content_hash" not in build_chunk_payload(
            {"summary": "s"}, memory_id=uuid4(), chunk_index=0, chunk_text="t"
        )
        # A memory found through a chunk only: its fallback payload has no
        # chunk fields, content_hash included.
        out = collapse_chunk_hits([{"id": "p", "score": 0.5, "payload": payload}])
        assert "content_hash" not in out[0]["payload"]
