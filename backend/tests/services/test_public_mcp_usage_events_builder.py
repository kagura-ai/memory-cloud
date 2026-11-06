"""Row builder of the public MCP usage events (#2678): privacy and observations."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from services.public_mcp_usage_events import (
    PUBLIC_TELEMETRY_MAX_DOCUMENTS,
    UsageCursor,
    UsageEventInput,
    build_gap_row,
    build_usage_event_row,
    decode_cursor,
    doc_key,
    encode_cursor,
    normalise_client_family,
    normalise_error_code,
    normalise_tool,
)
from utils.exceptions import CursorInvalidError

KEY = "k" * 40
ENDPOINT = UUID("0b6f6f0e-1111-4c1d-8a2b-000000000001")
RESOURCE = UUID("0b6f6f0e-4444-4c1d-8a2b-000000000001")
STARTED = datetime(2026, 11, 6, 9, 0, 0, tzinfo=UTC)


def _input(**overrides) -> UsageEventInput:
    values = {
        "endpoint_id": ENDPOINT,
        "workspace_id": UUID("0b6f6f0e-2222-4c1d-8a2b-000000000001"),
        "context_id": UUID("0b6f6f0e-3333-4c1d-8a2b-000000000001"),
        "resource_pk": RESOURCE,
        "resource_id": "product-catalog",
        "tool": "recall",
        "outcome": "success",
        "era": "session",
        "started_at": STARTED,
        "duration_ms": 12,
        "protocol_version": "2025-06-18",
        "result_count": 5,
    }
    values.update(overrides)
    return UsageEventInput(**values)


def _build(enabled: bool = False, **overrides):
    return build_usage_event_row(_input(**overrides), key=KEY, session_details_enabled=enabled)


def test_recall_chunks_collapse_by_document():
    built = _build(doc_ids=("doc-a", "doc-a", "doc-b", "doc-a", "doc-b"))
    docs = built.row["documents"]
    assert docs == [
        {"doc_key": doc_key(RESOURCE, "doc-a", KEY), "chunks": 3, "rank": 1},
        {"doc_key": doc_key(RESOURCE, "doc-b", KEY), "chunks": 2, "rank": 3},
    ]
    assert built.row["documents_truncated"] is False
    assert dict(built.doc_keys) == {
        doc_key(RESOURCE, "doc-a", KEY): "doc-a",
        doc_key(RESOURCE, "doc-b", KEY): "doc-b",
    }


def test_no_documents_is_null():
    built = _build(tool="list_tags", doc_ids=())
    assert built.row["documents"] is None
    assert built.doc_keys == ()


def test_empty_doc_ids_are_skipped():
    built = _build(doc_ids=("", "doc-a"))
    assert [d["rank"] for d in built.row["documents"]] == [2]


def test_cap_at_25_documents_sets_truncated():
    built = _build(doc_ids=tuple(f"doc-{i}" for i in range(30)))
    assert len(built.row["documents"]) == PUBLIC_TELEMETRY_MAX_DOCUMENTS == 25
    assert built.row["documents_truncated"] is True
    assert len(built.doc_keys) == 25
    # 25 observations stay far below the 4 KB JSONB CHECK.
    assert len(json.dumps(built.row["documents"]).encode()) < 4096


def test_reference_initial_and_continuation_share_the_doc_key():
    first = _build(tool="reference", doc_ids=("doc-a",), result_count=1)
    more = _build(tool="reference", doc_ids=("doc-a",), result_count=1, continuation=True)
    assert first.row["continuation"] is False
    assert more.row["continuation"] is True
    assert first.row["documents"] == more.row["documents"]


def test_doc_key_is_scoped_to_the_resource_and_the_key():
    a = doc_key(RESOURCE, "SKU-001", KEY)
    assert len(a) == 32
    assert int(a, 16) >= 0
    assert doc_key(uuid4(), "SKU-001", KEY) != a
    assert doc_key(RESOURCE, "SKU-001", "other-key" * 4) != a
    assert doc_key(RESOURCE, "SKU-001", KEY) == a


@pytest.mark.parametrize(
    ("requested", "stored"),
    [
        ("recall", "recall"),
        ("reference", "reference"),
        ("explore", "explore"),
        ("list_tags", "list_tags"),
        ("get_context_info", "get_context_info"),
        ("remember", "other"),
        ("forget", "other"),
        ("Recall", "other"),
        ("x" * 64, "other"),
        (None, "other"),
    ],
)
def test_tool_is_one_of_the_public_names_or_other(requested, stored):
    assert normalise_tool(requested) == stored
    assert _build(tool=requested, outcome="denied").row["tool"] == stored


@pytest.mark.parametrize(
    ("code", "stored"),
    [
        ("unknown_tool", "unknown_tool"),
        ("temporarily_unavailable", "temporarily_unavailable"),
        ("timeout", "timeout"),
        ("Bad-Code", "other"),
        ("x" * 65, "other"),
        ("", "other"),
        (None, None),
    ],
)
def test_error_code_normalisation(code, stored):
    assert normalise_error_code(code) == stored


def test_error_code_is_null_on_success_and_degraded():
    assert _build(outcome="success", error_code="timeout").row["error_code"] is None
    assert _build(outcome="degraded", error_code="timeout").row["error_code"] is None
    assert _build(outcome="error", error_code="timeout").row["error_code"] == "timeout"


def test_unknown_values_are_null_not_sentinels():
    built = _build(tool="get_context_info", result_count=None, protocol_version=None)
    assert built.row["result_count"] is None
    assert built.row["protocol_version"] is None
    assert built.row["documents"] is None


def test_protocol_version_must_look_like_one():
    assert _build(protocol_version="2026-07-28").row["protocol_version"] == "2026-07-28"
    assert _build(protocol_version="my-client-build-7").row["protocol_version"] is None


def test_occurred_at_is_naive_utc_of_the_start():
    row = _build(started_at=datetime(2026, 11, 6, 18, 0, tzinfo=UTC)).row
    assert row["occurred_at"] == datetime(2026, 11, 6, 18, 0)
    assert row["occurred_at"].tzinfo is None


def test_session_details_off_stores_neither():
    row = _build(enabled=False, session_id="sess-raw-123", client_name="Example Client").row
    assert row["session_key"] is None
    assert row["client_family"] is None


def test_session_details_on():
    a = _build(enabled=True, session_id="sess-raw-123", client_name="Example Client/1.2 <x>").row
    assert len(a["session_key"]) == 32
    assert a["client_family"] == "exampleclient1.2x"
    same_day = _build(
        enabled=True,
        session_id="sess-raw-123",
        started_at=datetime(2026, 11, 6, 23, 59, tzinfo=UTC),
    ).row
    next_day = _build(
        enabled=True,
        session_id="sess-raw-123",
        started_at=datetime(2026, 11, 7, 0, 1, tzinfo=UTC),
    ).row
    other_endpoint = _build(enabled=True, session_id="sess-raw-123", endpoint_id=uuid4()).row
    assert same_day["session_key"] == a["session_key"]
    assert next_day["session_key"] != a["session_key"]
    assert other_endpoint["session_key"] != a["session_key"]


def test_session_details_never_on_the_stateless_era():
    row = _build(enabled=True, era="stateless", session_id="s", client_name="Example").row
    assert row["session_key"] is None
    assert row["client_family"] is None


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("Example Client", "exampleclient"),
        ("ex_ample-Tool.v2", "ex_ample-tool.v2"),
        ("x" * 50, "x" * 32),
        ("!!!", None),
        ("", None),
        (None, None),
    ],
)
def test_client_family_normalisation(name, family):
    assert normalise_client_family(name) == family


def test_row_carries_nothing_the_request_sent():
    raw_session = "SESSION-9f8e7d6c-distinctive"
    built = build_usage_event_row(
        _input(
            tool="remember",
            outcome="denied",
            error_code="unknown_tool",
            doc_ids=("DOC-ID-distinctive-42",),
            session_id=raw_session,
            client_name="Distinctive Client Name",
        ),
        key=KEY,
        session_details_enabled=False,
    )
    dumped = json.dumps(built.row, default=str)
    for value in (
        raw_session,
        "DOC-ID-distinctive-42",
        "Distinctive Client Name",
        "remember",
    ):
        assert value not in dumped
    with_details = build_usage_event_row(
        _input(doc_ids=("DOC-ID-distinctive-42",), session_id=raw_session, client_name="C"),
        key=KEY,
        session_details_enabled=True,
    )
    dumped = json.dumps(with_details.row, default=str)
    assert raw_session not in dumped
    assert "DOC-ID-distinctive-42" not in dumped


def test_gap_row_shape():
    start = datetime(2026, 11, 6, 9, 0)
    end = datetime(2026, 11, 6, 9, 1)
    row = build_gap_row(dropped_count=3, started_at=start, ended_at=end, reason="queue_full")
    assert row["kind"] == "gap"
    assert row["dropped_count"] == 3
    assert row["gap_started_at"] == start
    assert row["gap_ended_at"] == end
    assert row["workspace_id"] is None
    assert row["tool"] is None
    assert set(row) == set(_build().row)


def test_cursor_round_trip():
    cursor = UsageCursor(710444, 42)
    encoded = encode_cursor(cursor)
    assert "=" not in encoded
    assert decode_cursor(encoded) == cursor


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-base64!!",
        "e30",  # {}
        encode_cursor(UsageCursor(1, 2)).replace("e", "f", 1),
        "eyJ2IjoyLCJ0IjoxLCJpIjoyfQ",  # v=2
        "eyJ2IjoxLCJ0IjotMSwiaSI6Mn0",  # t=-1
        "eyJ2IjoxLCJ0IjoiMSIsImkiOjJ9",  # t="1"
        "a" * 300,
    ],
)
def test_bad_cursors_are_refused(bad):
    with pytest.raises(CursorInvalidError) as exc:
        decode_cursor(bad)
    assert exc.value.status_code == 400
    assert exc.value.error_code == "cursor_invalid"
