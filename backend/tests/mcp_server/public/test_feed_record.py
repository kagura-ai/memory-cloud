"""The one definition of an unedited feed record on the public surface (#3032).

A row is projected with its record's title or question, URL and kind only
when it is the indexer's row, never edited, and its content is a record of the
feed contract in shape. Every other row keeps today's output.
"""

from __future__ import annotations

import json
import typing
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest

from mcp_server.public import feed_record
from mcp_server.public.feed_record import (
    FEED_RECORD_MAX_BYTES,
    FEED_RECORD_SOURCE,
    FeedRecord,
    feed_content_column,
    feed_record_of,
    parse_feed_record,
    public_source_url,
)
from models import sync_feed_schemas as contract
from models.memory import Memory


def _product(**over: Any) -> dict[str, Any]:
    record = {
        "record_type": "product",
        "external_id": "sku-1",
        "title": "Trail shoe",
        "url": "https://example.com/p/1",
        "description": "A light shoe.",
    }
    record.update(over)
    return record


def _faq(**over: Any) -> dict[str, Any]:
    record = {
        "record_type": "faq",
        "external_id": "faq-1",
        "question": "How do I return a shoe?",
        "answer": "Within 30 days.",
        "url": "https://example.com/faq/returns",
    }
    record.update(over)
    return record


def _json(record: Any) -> str:
    return json.dumps(record, ensure_ascii=False)


def _row(content: str | None, *, source: str | None = FEED_RECORD_SOURCE, version: int | None = 1):
    m = Memory(
        id=uuid4(),
        workspace_id=uuid4(),
        context_id=uuid4(),
        user_id="owner-1",
        summary="[catalog] sync:abc v1",
        content=content,
        type="resource_data",
        importance=0.5,
        scope="persistent",
        created_at=datetime(2026, 11, 1, 12, 0, 0),
    )
    m.source = source
    m.version = version
    return m


# ----------------------------------------------------------------------------
# The rule
# ----------------------------------------------------------------------------


def test_product_record_parses_to_kind_label_and_url() -> None:
    assert parse_feed_record(_json(_product())) == FeedRecord(
        kind="product", label="Trail shoe", url="https://example.com/p/1"
    )


def test_faq_record_parses_to_kind_question_and_url() -> None:
    assert parse_feed_record(_json(_faq())) == FeedRecord(
        kind="faq", label="How do I return a shoe?", url="https://example.com/faq/returns"
    )


def test_faq_without_url_is_recognised_without_url() -> None:
    record = _faq()
    del record["url"]
    assert parse_feed_record(_json(record)) == FeedRecord(
        kind="faq", label="How do I return a shoe?", url=None
    )


def test_label_is_one_line_and_uncut() -> None:
    title = "Trail\nshoe\t  for\r\nrunners " + "word " * 200
    parsed = parse_feed_record(_json(_product(title=title)))
    assert parsed is not None
    assert "\n" not in parsed.label and "\t" not in parsed.label and "\r" not in parsed.label
    assert parsed.label.startswith("Trail shoe for runners word")
    assert len(parsed.label) > 500  # the callers cut; the helper keeps the whole label


# ----------------------------------------------------------------------------
# Not a record: today's output
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "Full product text",
        "{not json",
        _json([_product()]),
        _json("a string"),
        _json({k: v for k, v in _product().items() if k != "record_type"}),
        _json(_product(record_type="article")),
        _json(_product(record_type=["product"])),
        _json(_product(record_type=None)),
        _json({k: v for k, v in _product().items() if k != "title"}),
        _json(_product(title="")),
        _json(_product(title=" \n\t ")),
        _json(_product(title=7)),
        _json(_faq(question=None)),
        "[" * 30_000,  # nesting deeper than the parser allows, within the size
    ],
)
def test_not_a_record(content) -> None:
    assert parse_feed_record(content) is None


def test_content_over_the_record_size_is_not_a_record() -> None:
    base = _product(description="")
    room = FEED_RECORD_MAX_BYTES - len(_json(base).encode())
    exact = _product(description="x" * room)
    assert len(_json(exact).encode()) == FEED_RECORD_MAX_BYTES
    assert parse_feed_record(_json(exact)) is not None
    over = _product(description="x" * (room + 1))
    assert parse_feed_record(_json(over)) is None
    # Multi-byte characters count as bytes, not as characters.
    wide = _product(description="é" * (room // 2 + 1))
    assert len(_json(wide)) <= FEED_RECORD_MAX_BYTES < len(_json(wide).encode())
    assert parse_feed_record(_json(wide)) is None


def test_non_string_content_is_not_a_record() -> None:
    assert parse_feed_record(b'{"record_type": "faq"}') is None  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
# Contract shape: a look-alike row keeps today's output
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        _product(color="red"),  # one key outside the closed set
        _faq(title="A product field on a FAQ"),
        {k: v for k, v in _product().items() if k != "external_id"},
        {k: v for k, v in _product().items() if k != "url"},  # product requires url
        {k: v for k, v in _faq().items() if k != "answer"},  # FAQ requires answer
        _product(price=12),  # a non-string value
        _product(external_id=1),
        _faq(answer={"text": "nested"}),
        _product(url=None),
    ],
)
def test_look_alike_is_not_a_record(record) -> None:
    assert parse_feed_record(_json(record)) is None


# ----------------------------------------------------------------------------
# URL rule: the record is still recognised, only the URL is dropped
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/p/1",
        "https://user@example.com/p/1",
        "https://user:pw@example.com/p/1",
        "https://example.com/p 1",
        "https://example.com/p/1\n",
        "https:///no-host",
        "ftp://example.com/p/1",
        "/relative",
        "https://[broken",
        "https://example.com/" + "a" * 2_030,
    ],
)
def test_url_failing_the_rule_is_dropped(url) -> None:
    parsed = parse_feed_record(_json(_product(url=url)))
    assert parsed == FeedRecord(kind="product", label="Trail shoe", url=None)


def test_url_at_the_length_limit_is_kept() -> None:
    url = "https://example.com/" + "a" * (2_048 - len("https://example.com/"))
    assert len(url) == 2_048
    assert parse_feed_record(_json(_product(url=url))) == FeedRecord(
        kind="product", label="Trail shoe", url=url
    )


# ----------------------------------------------------------------------------
# Row gate: edited and hand-written rows are untouched
# ----------------------------------------------------------------------------


def test_unedited_indexer_row_is_a_feed_record() -> None:
    assert feed_record_of(_row(_json(_product()))) == parse_feed_record(_json(_product()))


@pytest.mark.parametrize(
    ("source", "version"),
    [
        (FEED_RECORD_SOURCE, 2),
        ("remember", 1),
        (None, 1),
        (None, None),  # a row with neither attribute set (the existing fakes)
        (FEED_RECORD_SOURCE, None),
    ],
)
def test_edited_or_other_rows_are_not(source, version) -> None:
    assert feed_record_of(_row(_json(_product()), source=source, version=version)) is None


def test_existing_fakes_without_source_or_version_are_not() -> None:
    m = Memory(summary="s", content=_json(_product()))
    assert feed_record_of(m) is None


# ----------------------------------------------------------------------------
# Pins: the copy cannot drift from the contract
# ----------------------------------------------------------------------------


def test_source_is_the_indexer_source() -> None:
    from services.resource_indexer import INDEXER_SOURCE

    assert FEED_RECORD_SOURCE == INDEXER_SOURCE


def test_kinds_are_the_feed_kinds_and_the_registered_sets() -> None:
    """Every registered version's kinds; a kind's fields are the union over the versions."""
    kinds = set(typing.get_args(contract.FeedConfig.model_fields["kind"].annotation))
    assert set(feed_record._KINDS) == kinds
    for kind in kinds:
        sets = [entry.kinds[kind] for entry in contract.REGISTRY.values() if kind in entry.kinds]
        assert feed_record._KINDS[kind][:2] == (
            frozenset().union(*(allowed for allowed, _ in sets)),
            frozenset.intersection(*(required for _, required in sets)),
        )
    assert contract.SCHEMA_V1.kinds["product"] == (contract.PRODUCT, contract.PRODUCT_REQUIRED)
    assert contract.SCHEMA_V1.kinds["faq"] == (contract.FAQ, contract.FAQ_REQUIRED)
    assert FEED_RECORD_MAX_BYTES == contract.MAX_RECORD_BYTES


@pytest.mark.parametrize(
    ("kind", "fields", "label"),
    [
        (
            "product",
            {
                "title": "Trail shoe",
                "url": "https://example.com/p/1",
                "price": "12.50",
                "currency": "EUR",
                "availability": "in_stock",
            },
            "Trail shoe",
        ),
        ("faq", {"question": "Returns?", "answer": "Within 30 days."}, "Returns?"),
    ],
)
def test_a_contract_record_parses(kind, fields, label) -> None:
    payload = contract.FeedRecord(external_id="id-1", fields=fields).public_payload(
        kind, contract.SCHEMA_V1
    )
    parsed = parse_feed_record(json.dumps(payload, ensure_ascii=False))
    assert parsed is not None
    assert (parsed.kind, parsed.label, parsed.url) == (kind, label, fields.get("url"))


def test_url_rule_is_one_function() -> None:
    from mcp_server.public import tools
    from services import observability_documents

    assert tools._source_url is public_source_url
    assert observability_documents.public_source_url is public_source_url


def test_feed_content_column_gates_source_version_and_size() -> None:
    column = feed_content_column()
    assert column.name == "feed_content"
    sql = str(column.compile(compile_kwargs={"literal_binds": True}))
    assert "CASE WHEN" in sql
    assert "memories.source = 'resource_ingest'" in sql
    assert "memories.version = 1" in sql
    assert "octet_length(memories.content) <= 32768" in sql
    assert "THEN memories.content" in sql
