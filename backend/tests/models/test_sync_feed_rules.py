"""Record schema versions 1 and 2: definitions, value rules and reason codes (#3055).

The specification is frozen and senders build against it, so these vectors pin
the hashes, the order of the checks and the reason each refusal reports. A
refused record reports the first failing rule: unknown keys in the record's key
order, missing keys in code-point order, each value in the record's key order
(length, characters, blank, format), the rules across fields, the record size.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import typing
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.responses import JSONResponse

from models.management_schemas import digest
from models.sync_feed_rules import REASONS, RecordRefusal, RefusalReason
from models.sync_feed_schemas import (
    MAX_RECORD_BYTES,
    REGISTRY,
    SCHEMA_V1,
    SCHEMA_V2,
    FeedRecord,
    stored_schema,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
V1_HASH = "bdb55d3bdbbbb72a92a33fb96e6a2dc60ccbaf5b09638dcc1f1619d016dfec0d"
V2_HASH = "8ddfbbbde388dd0ac44899e10be2a6c26d0a4521098a9bd59f74784a59bb3834"
SCHEMAS = {1: SCHEMA_V1, 2: SCHEMA_V2}
PRODUCT = {"title": "T", "url": "https://a.example/p"}
PAGE = {"title": "T", "url": "https://a.example/p", "body": "B"}
REVIEW = {"body": "Good."}


def outcome(version: int, kind: str, fields: dict[str, str], external_id: str = "e-1"):
    """``None`` when accepted, else ``(reason, field)``."""
    try:
        FeedRecord(external_id=external_id, fields=fields).public_payload(kind, SCHEMAS[version])
    except RecordRefusal as refusal:
        return refusal.reason, refusal.field
    return None


# ----------------------------------------------------------------------------
# Definitions and the published answers
# ----------------------------------------------------------------------------


def test_both_digests_are_the_frozen_values():
    assert SCHEMA_V1.schema_hash == digest(SCHEMA_V1.definitions) == V1_HASH
    assert SCHEMA_V2.schema_hash == digest(SCHEMA_V2.definitions) == V2_HASH
    assert (
        hashlib.sha256(
            json.dumps(
                SCHEMA_V2.definitions,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        == V2_HASH
    )
    assert set(REGISTRY) == {(1, V1_HASH), (2, V2_HASH)}


def test_definition_counts_kinds_and_key_order():
    assert len(SCHEMA_V1.definitions) == 16 and len(SCHEMA_V2.definitions) == 36
    for entry, indexed in ((SCHEMA_V1, 6), (SCHEMA_V2, 15)):
        names = [d["name"] for d in entry.definitions]
        assert names == sorted(names)
        assert all(
            list(d) == ["name", "type", "classification", "required", "index_hint"]
            for d in entry.definitions
        )
        assert sum(d["index_hint"] == "fulltext,vector" for d in entry.definitions) == indexed
        assert {d["name"] for d in entry.definitions if d["required"]} == {
            "record_type",
            "external_id",
        }
    assert list(SCHEMA_V1.kinds) == ["product", "faq"]
    assert list(SCHEMA_V2.kinds) == ["product", "faq", "page", "review"]
    assert len(SCHEMA_V2.kinds["product"][0]) == 22
    assert len(SCHEMA_V2.kinds["review"][0]) == 15
    assert SCHEMA_V2.kinds["page"][1] == {"title", "url", "body"}
    assert SCHEMA_V2.kinds["review"][1] == {"body"}
    assert set(SCHEMA_V1.definitions[0]) and SCHEMA_V1.names <= SCHEMA_V2.names


@pytest.mark.parametrize(
    ("version", "name"), [(1, "sync-schema-v1.json"), (2, "sync-schema-v2.json")]
)
def test_the_schema_answer_is_the_golden_file_byte_for_byte(version, name):
    body = JSONResponse(SCHEMAS[version].answer()).body
    assert body == (FIXTURES / name).read_bytes().removesuffix(b"\n")
    sha = {
        1: "936fd4e0746941f8db3500cb5a91b6252a77b559d9bd48cb41eb36a6f95c6f32",
        2: "3497fc0e687747752f0b192ca2607890436a102fea195083ca3cf074fa66df79",
    }[version]
    assert hashlib.sha256(body).hexdigest() == sha
    assert len(body) == {1: 1864, 2: 4655}[version]


def test_the_new_limits_keys_appear_on_version_two_only():
    v1, v2 = SCHEMA_V1.answer()["limits"], SCHEMA_V2.answer()["limits"]
    assert list(v1) == list(v2)[:8]
    assert list(v2)[8:] == ["record_bytes", "field_chars", "attributes"]
    assert v2["record_bytes"] == MAX_RECORD_BYTES
    assert list(v2["field_chars"]) == sorted(SCHEMA_V2.names - {"record_type", "external_id"})
    assert v2["attributes"] == {
        "bytes": 8192,
        "keys": 50,
        "key_chars": 64,
        "values_per_key": 20,
        "value_chars": 2048,
    }


def test_the_answer_never_shares_the_registry_objects():
    answer = SCHEMA_V2.answer()
    answer["fields"][0]["name"] = "changed"
    answer["limits"]["field_chars"]["title"] = 1
    assert SCHEMA_V2.schema_hash == digest(SCHEMA_V2.definitions) == V2_HASH
    assert SCHEMA_V2.answer()["limits"]["field_chars"]["title"] == 2048


def test_a_stored_row_resolves_by_its_pair_only():
    class Row:
        def __init__(self, version, definitions):
            self.schema_version, self.field_definitions = version, definitions

    assert stored_schema(Row(1, SCHEMA_V1.stored_definitions())) is SCHEMA_V1
    assert stored_schema(Row(2, SCHEMA_V2.stored_definitions())) is SCHEMA_V2
    assert stored_schema(Row(2, SCHEMA_V1.stored_definitions())) is None
    assert stored_schema(Row(1, SCHEMA_V2.stored_definitions())) is None
    assert stored_schema(Row(3, [{"name": "x"}])) is None
    assert stored_schema(None) is None


def test_reason_codes_are_the_closed_list_of_thirty_seven():
    assert len(REASONS) == len(set(REASONS)) == 37
    assert REASONS == typing.get_args(RefusalReason)
    assert REASONS[0] == "unknown_field" and REASONS[-1] == "record_too_large"


# ----------------------------------------------------------------------------
# Keys
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "kind", "fields", "expected"),
    [
        (1, "product", {**PRODUCT, "color": "red"}, ("unknown_field", None)),
        (1, "product", {**PRODUCT, "body": "x"}, ("unknown_field", None)),
        (1, "product", {**PRODUCT, "question": "Q"}, ("unknown_field", "question")),
        (2, "product", {**PRODUCT, "body": "x"}, ("unknown_field", "body")),
        (2, "product", {**PRODUCT, "record_type": "x"}, ("unknown_field", "record_type")),
        (2, "product", {"zzz": "1", "body": "x", **PRODUCT}, ("unknown_field", None)),
        (2, "faq", {"question": "Q", "answer": "A", "title": "T"}, ("unknown_field", "title")),
        (2, "page", {"title": "T"}, ("missing_field", "body")),
        (2, "page", {"body": "B"}, ("missing_field", "title")),
        (2, "page", {"title": "T", "body": "B"}, ("missing_field", "url")),
        (2, "review", {"title": "T"}, ("missing_field", "body")),
        (2, "review", {}, ("missing_field", "body")),
        (1, "faq", {"question": "Q"}, ("missing_field", "answer")),
        (2, "review", REVIEW, None),
        (2, "page", {**PAGE, "heading": "H"}, None),
    ],
)
def test_keys(version, kind, fields, expected):
    assert outcome(version, kind, fields) == expected


def test_a_kind_outside_the_version_is_refused():
    with pytest.raises(ValueError):
        FeedRecord(external_id="e", fields=PAGE).public_payload("page", SCHEMA_V1)


# ----------------------------------------------------------------------------
# Length, characters, blank
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({**PRODUCT, "availability_note": "x" * 200}, None),
        ({**PRODUCT, "availability_note": "x" * 201}, ("too_long", "availability_note")),
        ({**PRODUCT, "color": "x" * 2049}, ("too_long", "color")),
        ({**PRODUCT, "description": "x" * 16001}, ("too_long", "description")),
        ({**PRODUCT, "attributes": "x" * 8193}, ("too_long", "attributes")),
        ({**PRODUCT, "title": "a\x7fb\x85c\u2028"}, None),
        ({**PRODUCT, "color": "a\x7fb"}, ("control_character", "color")),
        ({**PRODUCT, "color": "a\x85"}, ("control_character", "color")),
        ({**PRODUCT, "material": "a\u2029b"}, ("control_character", "material")),
        ({**PRODUCT, "size": "a\tb"}, ("control_character", "size")),
        ({**PRODUCT, "title": "a\x00"}, ("control_character", "title")),
        ({**PRODUCT, "product_highlights": "a\tb"}, ("control_character", "product_highlights")),
        ({**PRODUCT, "product_highlights": "a\nb"}, None),
        ({**PRODUCT, "color": " "}, ("blank", "color")),
        ({**PRODUCT, "color": "\u3000\u00a0"}, ("blank", "color")),
        ({**PRODUCT, "color": ""}, ("blank", "color")),
        ({**PRODUCT, "color": "\u200b"}, None),
        ({**PRODUCT, "title": "\u00a0"}, ("blank", "title")),
        ({**PRODUCT, "title": "\x1c"}, ("control_character", "title")),
        # Value checks run in the record's key order: the first bad value wins.
        ({**PRODUCT, "color": " ", "size": "a\x00"}, ("blank", "color")),
        ({**PRODUCT, "size": "a\x00", "color": " "}, ("control_character", "size")),
    ],
)
def test_length_characters_and_blank_of_a_product(fields, expected):
    assert outcome(2, "product", fields) == expected


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        ("page", {**PAGE, "body": "a\tb\nc\rd"}, None),
        ("page", {**PAGE, "body": "a\x0bb"}, ("control_character", "body")),
        ("page", {**PAGE, "body": "a\u2028b"}, ("control_character", "body")),
        ("page", {**PAGE, "body": "x" * 16001}, ("too_long", "body")),
        ("page", {**PAGE, "heading": "a\nb"}, ("control_character", "heading")),
        ("review", {**REVIEW, "product_name": "Shoe\tA\r\nB"}, None),
        ("review", {**REVIEW, "product_name": "a\x7f"}, ("control_character", "product_name")),
        ("review", {**REVIEW, "reviewed_at": "x" * 41}, ("too_long", "reviewed_at")),
        ("review", {"body": " \n "}, ("blank", "body")),
    ],
)
def test_text_classes_of_pages_and_reviews(kind, fields, expected):
    assert outcome(2, kind, fields) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a\nb\nc", None),
        ("a\n\nb", "empty_line"),
        ("\na", "empty_line"),
        ("a\n", "empty_line"),
        (" a", "line_whitespace"),
        ("a \nb", "line_whitespace"),
        ("a\nb\u3000", "line_whitespace"),
        ("a\n\u00a0b", "line_whitespace"),
    ],
)
def test_list_fields(value, expected):
    for name in ("product_highlights", "product_types", "product_details"):
        result = outcome(2, "product", {**PRODUCT, name: value})
        assert result == (None if expected is None else (expected, name))


# ----------------------------------------------------------------------------
# Formats
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://shop.example/p", None),
        ("HTTPS://shop.example/p", None),
        ("http://shop.example/p", "invalid_url"),
        ("https://user@shop.example/p", "invalid_url"),
        ("https://:pw@shop.example/p", "invalid_url"),
        ("https:///p", "invalid_url"),
        ("https://[::1", "invalid_url"),
        ("https://shop.example/a b", "invalid_url"),
        ("https://shop.example/a\u3000", "invalid_url"),
        ("https://shop.example/a\x85", "control_character"),
    ],
)
def test_product_url(value, expected):
    result = outcome(2, "review", {**REVIEW, "product_url": value})
    assert result == (None if expected is None else (expected, "product_url"))


def test_the_url_rule_of_version_one_keeps_accepting_del_and_refusing_c1_whitespace():
    assert outcome(1, "product", {**PRODUCT, "url": "https://a.example/\x7f"}) is None
    assert outcome(2, "product", {**PRODUCT, "url": "https://a.example/\x7f"}) is None
    for version in (1, 2):
        assert outcome(version, "product", {**PRODUCT, "url": "https://a.example/\x85"}) == (
            "invalid_url",
            "url",
        )


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({**PRODUCT, "price": "12.50", "currency": "JPY"}, None),
        ({**PRODUCT, "price": "12.50"}, ("requires_field", "currency")),
        ({**PRODUCT, "currency": "JPY"}, ("requires_field", "price")),
        ({**PRODUCT, "price": "01", "currency": "JPY"}, ("invalid_price", "price")),
        ({**PRODUCT, "price": "1", "currency": "jpy"}, ("invalid_currency", "currency")),
        ({**PRODUCT, "availability": "sold"}, ("invalid_enum", "availability")),
        ({**PRODUCT, "condition": "New"}, ("invalid_enum", "condition")),
        ({**PRODUCT, "sale_price": "5"}, ("requires_field", "price")),
        ({**PRODUCT, "sale_price": "5", "price": "9"}, ("requires_field", "currency")),
        ({**PRODUCT, "sale_price": "5", "price": "9", "currency": "EUR"}, None),
        # Not compared with price.
        ({**PRODUCT, "sale_price": "50", "price": "9", "currency": "EUR"}, None),
        (
            {**PRODUCT, "sale_price": "1.23456", "price": "9", "currency": "EUR"},
            ("invalid_price", "sale_price"),
        ),
        (
            {**PRODUCT, "sale_price": "5 ", "price": "9", "currency": "EUR"},
            ("invalid_price", "sale_price"),
        ),
        (
            {
                **PRODUCT,
                "price": "9",
                "currency": "EUR",
                "sale_price_effective_date": "2026-12-01T00:00:00Z/2026-12-02T00:00:00Z",
            },
            ("requires_field", "sale_price"),
        ),
    ],
)
def test_prices(fields, expected):
    assert outcome(2, "product", fields) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", None),
        ("false", None),
        ("True", "invalid_enum"),
        ("1", "invalid_enum"),
        ("yes", "invalid_enum"),
    ],
)
def test_review_flags(value, expected):
    for name in ("incentivized", "verified_purchase"):
        result = outcome(2, "review", {**REVIEW, name: value})
        assert result == (None if expected is None else (expected, name))


VALID_DATE_TIMES = [
    "2026-11-28T09:00:00Z",
    "2026-11-28T09:00:00.1Z",
    "2026-11-28T09:00:00.123456+09:00",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59.999999+00:00",
    "2024-02-29T23:59:59-23:59",
    "2000-02-29T00:00:00+23:59",
    "2026-11-28T09:00:00+00:00",
]
INVALID_DATE_TIMES = [
    "2026-11-28 09:00:00Z",
    "2026-11-28T09:00Z",
    "20261128T090000Z",
    "2026-W48-6T00:00:00Z",
    "2026-332T00:00:00Z",
    "2026-11-28T09:00:00",
    "2026-11-28T09:00:00.1234567Z",
    "2026-11-28T09:00:00.Z",
    "\uff12\uff10\uff12\uff16-11-28T09:00:00Z",
    "0000-01-01T00:00:00Z",
    "2026-13-01T00:00:00Z",
    "2026-00-01T00:00:00Z",
    "2023-02-29T00:00:00Z",
    "1900-02-29T00:00:00Z",
    "2026-04-31T00:00:00Z",
    "2026-11-28T24:00:00Z",
    "2026-11-28T23:60:00Z",
    "2026-11-28T23:59:60Z",
    "2026-11-28T00:00:00-00:00",
    "2026-11-28T00:00:00+24:00",
    "2026-11-28T00:00:00+09:60",
    "2026-11-28t00:00:00z",
    "2026-11-28T00:00:00z",
    "2026-11-28T00:00:00+0900",
    " 2026-11-28T00:00:00Z",
    "2026-11-28T00:00:00Z\n",
]


@pytest.mark.parametrize("value", VALID_DATE_TIMES)
def test_valid_date_times(value):
    assert outcome(2, "review", {**REVIEW, "reviewed_at": value}) is None


@pytest.mark.parametrize("value", INVALID_DATE_TIMES)
def test_invalid_date_times(value):
    expected = {
        " 2026-11-28T00:00:00Z": "invalid_date_time",
        "2026-11-28T00:00:00Z\n": "control_character",
    }.get(value, "invalid_date_time")
    assert outcome(2, "review", {**REVIEW, "reviewed_at": value}) == (expected, "reviewed_at")


def test_the_longest_valid_date_time_has_thirty_two_characters():
    longest = "2026-11-28T09:00:00.123456+09:00"
    assert len(longest) == 32
    assert outcome(2, "review", {**REVIEW, "reviewed_at": longest}) is None


SALE = {**PRODUCT, "price": "9", "currency": "EUR", "sale_price": "5"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-12-01T00:00:00+09:00/2026-12-31T23:59:59.999999+09:00", None),
        ("2026-12-01T00:00:00Z/2026-12-01T00:00:00.000001Z", None),
        ("2026-12-01T09:00:00+09:00/2026-12-01T00:00:00Z", "interval_order"),
        ("2026-12-02T00:00:00Z/2026-12-01T00:00:00Z", "interval_order"),
        ("2026-12-01T00:00:00Z", "invalid_interval"),
        ("2026-12-01T00:00:00Z/2026-12-02T00:00:00Z/", "invalid_interval"),
        ("x/2026-12-01T00:00:00Z", "invalid_date_time"),
        ("2026-12-01T00:00:00Z/", "invalid_date_time"),
        ("2026-12-01T00:00:00Z /2026-12-02T00:00:00Z", "invalid_date_time"),
        ("2026-12-01T00:00:00.123456+09:00/2026-12-31T23:59:59.999999+09:00X", "invalid_date_time"),
        ("x" * 81, "too_long"),
    ],
)
def test_sale_price_effective_date(value, expected):
    result = outcome(2, "product", {**SALE, "sale_price_effective_date": value})
    assert result == (None if expected is None else (expected, "sale_price_effective_date"))


def test_the_longest_valid_interval_has_sixty_five_characters():
    value = "2026-12-01T00:00:00.123456+09:00/2026-12-31T23:59:59.999999+09:00"
    assert len(value) == 65
    assert outcome(2, "product", {**SALE, "sale_price_effective_date": value}) is None


RATED = {**REVIEW, "rating": "4", "rating_min": "1", "rating_max": "5"}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, None),
        ({"rating_max": "10", "rating": "9.5"}, None),
        ({"rating_min": "0", "rating": "0"}, None),
        ({"rating": "5.0"}, None),
        ({"rating": "1.234"}, ("invalid_rating", "rating")),
        ({"rating": "05"}, ("invalid_rating", "rating")),
        ({"rating_max": "1234567"}, ("invalid_rating", "rating_max")),
        ({"rating_min": "-1"}, ("invalid_rating", "rating_min")),
        ({"rating_min": "5", "rating": "5"}, ("invalid_rating_range", "rating_max")),
        ({"rating_min": "5.0", "rating": "5"}, ("invalid_rating_range", "rating_max")),
        ({"rating_min": "6", "rating": "6"}, ("invalid_rating_range", "rating_max")),
        ({"rating": "5.01"}, ("rating_out_of_range", "rating")),
        ({"rating_max": "10", "rating": "10.01"}, ("rating_out_of_range", "rating")),
        ({"rating": "0.99"}, ("rating_out_of_range", "rating")),
    ],
)
def test_ratings(changes, expected):
    assert outcome(2, "review", {**RATED, **changes}) == expected


@pytest.mark.parametrize(
    ("present", "missing"),
    [
        (("rating",), "rating_min"),
        (("rating", "rating_min"), "rating_max"),
        (("rating_max",), "rating"),
        (("rating_min", "rating_max"), "rating"),
    ],
)
def test_the_rating_group_is_complete(present, missing):
    fields = {**REVIEW, **{name: RATED[name] for name in present}}
    assert outcome(2, "review", fields) == ("requires_field", missing)


# ----------------------------------------------------------------------------
# attributes
# ----------------------------------------------------------------------------


def attributes(value: str):
    result = outcome(2, "product", {**PRODUCT, "attributes": value})
    if result is not None:
        assert result[1] == "attributes"
        return result[0]
    return None


def test_a_canonical_attributes_value_is_accepted():
    assert attributes('{"fit":["slim"],"pattern":["solid","striped"]}') is None
    assert attributes('{"a1":["x"],"a_b":["y"]}') is None
    assert attributes('{"a":["\\"q\\"","b\\\\c","d/e","\u00e9"]}') is None
    assert attributes(json.dumps({"k": [str(i) for i in range(20)]}, separators=(",", ":"))) is None
    many = {f"k{i:02d}": ["v"] for i in range(50)}
    assert attributes(json.dumps(many, separators=(",", ":"), sort_keys=True)) is None
    assert attributes(json.dumps({"k" * 64: ["v"]}, separators=(",", ":"))) is None


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ('{"a":["' + "\u00e9" * 4095 + '"]}', "attributes_too_large"),
        ('{"a":["x"]', "attributes_invalid_json"),
        ('{"a":["x"],}', "attributes_invalid_json"),
        ("[" * 4000 + "]" * 4000, "attributes_invalid_json"),
        ('{"a":["x"],"a":["y"]}', "attributes_duplicate_key"),
        ('{"a":[{"b":1,"b":2}]}', "attributes_duplicate_key"),
        ("NaN", "attributes_not_object"),
        ("[]", "attributes_not_object"),
        ('"x"', "attributes_not_object"),
        ("[" * 100 + "]" * 100, "attributes_not_object"),
        ("{}", "attributes_empty"),
        (
            json.dumps({f"k{i:02d}": ["v"] for i in range(51)}, separators=(",", ":")),
            "attributes_too_many_keys",
        ),
        ('{"A":["x"]}', "attributes_invalid_key"),
        ('{"1a":["x"]}', "attributes_invalid_key"),
        ('{"a-b":["x"]}', "attributes_invalid_key"),
        ('{"\\ud800":["x"]}', "attributes_invalid_key"),
        (json.dumps({"k" * 65: ["v"]}, separators=(",", ":")), "attributes_invalid_key"),
        ('{"title":["x"]}', "attributes_reserved_key"),
        ('{"rating":["x"]}', "attributes_reserved_key"),
        ('{"external_id":["x"]}', "attributes_reserved_key"),
        ('{"a":"x"}', "attributes_value_not_list"),
        ('{"a":[]}', "attributes_value_count"),
        (
            json.dumps({"k": [str(i) for i in range(21)]}, separators=(",", ":")),
            "attributes_value_count",
        ),
        ('{"a":[1]}', "attributes_value_not_string"),
        ('{"a":[1.0]}', "attributes_value_not_string"),
        ('{"a":[true]}', "attributes_value_not_string"),
        ('{"a":[null]}', "attributes_value_not_string"),
        ('{"a":[NaN]}', "attributes_value_not_string"),
        ('{"a":[["x"]]}', "attributes_value_not_string"),
        ('{"a":["' + "x" * 2049 + '"]}', "attributes_value_too_long"),
        ('{"a":["x\\u0001"]}', "attributes_value_control"),
        ('{"a":["x\\n"]}', "attributes_value_control"),
        ('{"a":["x\\u2028"]}', "attributes_value_control"),
        ('{"a":[" "]}', "attributes_value_blank"),
        ('{"a":[""]}', "attributes_value_blank"),
        ('{"a":[" x"]}', "attributes_value_untrimmed"),
        ('{"a":["x\u3000"]}', "attributes_value_untrimmed"),
        ('{"a":["e\u0301"]}', "attributes_value_not_nfc"),
        ('{"a":["x","x"]}', "attributes_duplicate_value"),
        ('{"a": ["x"]}', "attributes_not_canonical"),
        (' {"a":["x"]}', "attributes_not_canonical"),
        ('{"b":["x"],"a":["y"]}', "attributes_not_canonical"),
        ('{"a_b":["y"],"a1":["x"]}', "attributes_not_canonical"),
        ('{"a":["d\\/e"]}', "attributes_not_canonical"),
        ('{"a":["\\u00e9"]}', "attributes_not_canonical"),
        ('{"a":["\\ud800"]}', "attributes_not_canonical"),
        ('{"a":["\\ud83d\\ude00"]}', "attributes_not_canonical"),
    ],
)
def test_attributes_reasons(value, reason):
    assert attributes(value) == reason


def test_attributes_string_rules_come_first():
    assert attributes('{"a":["x\ty"]}') == "control_character"
    assert attributes("   ") == "blank"
    assert attributes("x" * 8193) == "too_long"
    # 8,192 characters but more UTF-8 bytes.
    text = '{"a":["' + "\u00e9" * 4095 + '"]}'
    assert len(text) <= 8192 < len(text.encode())
    assert attributes(text) == "attributes_too_large"


def test_attributes_is_valid_only_for_products():
    assert outcome(2, "review", {**REVIEW, "attributes": '{"a":["x"]}'}) == (
        "unknown_field",
        "attributes",
    )


# ----------------------------------------------------------------------------
# Record size
# ----------------------------------------------------------------------------


def test_the_record_size_bound_is_exact():
    base = {"title": "T", "url": "https://a.example/p"}
    fits = {**base, "body": "\u3042" * 10_889}
    payload = {"record_type": "page", "external_id": "p-1", **fits}
    assert len(json.dumps(payload, ensure_ascii=False).encode()) == 32_768
    assert outcome(2, "page", fits, external_id="p-1") is None
    assert outcome(2, "page", {**base, "body": "\u3042" * 10_890}, external_id="p-1") == (
        "record_too_large",
        None,
    )


def test_the_record_size_counts_escapes_of_attributes():
    value = json.dumps({"a": ['"' * 2000, '"\\' * 1000]}, separators=(",", ":"))
    assert attributes(value) is None
    big = {**PRODUCT, "attributes": value, "description": "d" * 16_000, "product_details": "x"}
    payload = {"record_type": "product", "external_id": "e-1", **big}
    size = len(json.dumps(payload, ensure_ascii=False).encode())
    assert (outcome(2, "product", big) is None) == (size <= 32_768)


# ----------------------------------------------------------------------------
# Version 1 is unchanged; version 2 accepts every version-1 record
# ----------------------------------------------------------------------------


def previous_decision(kind: str, external_id: str, fields: dict[str, str]) -> bool:
    """v0.187.0's ``FeedRecord.public_payload`` decision, copied unchanged."""
    product = {
        "title", "url", "description", "image_url", "price", "currency", "availability",
        "brand", "sku", "gtin", "mpn", "condition",
    }  # fmt: skip
    allowed = product if kind == "product" else {"question", "answer", "url"}
    required = {"title", "url"} if kind == "product" else {"question", "answer"}
    try:
        if not required.issubset(fields) or not set(fields).issubset(allowed):
            raise ValueError
        for name, value in fields.items():
            limit = 16_000 if name in {"description", "answer"} else 2048
            if (
                not value.strip()
                or len(value) > limit
                or any(ord(c) < 32 and c not in "\n\r\t" for c in value)
            ):
                raise ValueError
            if name in {"url", "image_url"}:
                parsed = urlsplit(value)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or any(c.isspace() for c in value)
                ):
                    raise ValueError
        if ("price" in fields) != ("currency" in fields):
            raise ValueError
        if "price" in fields and not re.fullmatch(
            r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?", fields["price"]
        ):
            raise ValueError
        if "currency" in fields and not re.fullmatch(r"[A-Z]{3}", fields["currency"]):
            raise ValueError
        for name, choices in {
            "availability": {"in_stock", "out_of_stock", "preorder", "backorder"},
            "condition": {"new", "used", "refurbished"},
        }.items():
            if name in fields and fields[name] not in choices:
                raise ValueError
        payload = {"record_type": kind, "external_id": external_id, **fields}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 32_768:
            raise ValueError
    except ValueError:
        return False
    return True


def decisions(kind: str, fields: dict[str, str]) -> tuple[bool, bool, bool]:
    return (
        previous_decision(kind, "e-1", fields),
        outcome(1, kind, fields) is None,
        outcome(2, kind, fields) is None,
    )


def _code_points() -> list[str]:
    points = list(range(0x0000, 0x3100)) + [
        0xA0, 0x1680, 0x180E, 0x200B, 0x202F, 0x205F, 0x2060, 0x3000, 0xD7FF, 0xE000,
        0xFEFF, 0xFFFD, 0xFFFF, 0x1F600, 0x10FFFF,
    ]  # fmt: skip
    return [chr(c) for c in sorted(set(points))]


def test_single_code_points_decide_as_before_in_both_versions():
    shapes = (
        lambda c: c,
        lambda c: "a" + c,
        lambda c: c + "a",
        lambda c: c * 3,
        lambda c: "a" + c + "b",
    )
    for c in _code_points():
        for shape in shapes:
            value = shape(c)
            for fields, kind in (
                ({"title": value, "url": "https://a.example/p"}, "product"),
                ({"title": "T", "url": "https://a.example/p", "description": value}, "product"),
                ({"question": "Q", "answer": value}, "faq"),
            ):
                before, v1, v2 = decisions(kind, fields)
                assert before == v1, (hex(ord(c)), fields)
                assert not before or v2, (hex(ord(c)), fields)
        for url in ("https://a.example/" + c, "https://a" + c + ".example", c + "https://a.ex"):
            before, v1, v2 = decisions("product", {"title": "T", "url": url})
            assert before == v1, (hex(ord(c)), url)
            assert not before or v2, (hex(ord(c)), url)


def test_random_records_decide_as_before_and_stay_valid_under_version_two():
    rng = random.Random(3055)
    alphabet = [
        "a", "Z", "0", "9", ".", "/", ":", " ", "\t", "\n", "\r", "\x00", "\x1f", "\x7f",
        "\x85", "\xa0", "\u2028", "\u3000", "\u00e9", "https://", "s.example", "@", "-",
        "JPY", "12", "in_stock", "new",
    ]  # fmt: skip
    keys = {
        "product": [
            "title", "url", "description", "image_url", "price", "currency", "availability",
            "brand", "sku", "gtin", "mpn", "condition", "question", "color",
        ],
        "faq": ["question", "answer", "url", "title", "body"],
    }  # fmt: skip
    accepted = 0
    for _ in range(20_000):
        kind = rng.choice(["product", "faq"])
        fields: dict[str, str] = {}
        if kind == "product":
            fields["title"] = "T"
            fields["url"] = "https://s.example/p"
        else:
            fields["question"] = "Q"
            fields["answer"] = "A"
        for _ in range(rng.randint(0, 4)):
            name = rng.choice(keys[kind])
            fields[name] = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4)))
        if rng.random() < 0.3:
            fields.pop(rng.choice(list(fields)))
        before, v1, v2 = decisions(kind, fields)
        assert before == v1, fields
        assert not v1 or v2, fields
        accepted += v1
    assert accepted > 1000


def test_a_version_one_installation_returns_only_version_one_codes():
    v1_codes = {
        "unknown_field", "missing_field", "too_long", "control_character", "blank",
        "invalid_url", "invalid_price", "invalid_currency", "invalid_enum", "requires_field",
        "record_too_large",
    }  # fmt: skip
    samples = [
        {**PRODUCT, "color": "x"},
        {"title": "T"},
        {**PRODUCT, "title": "x" * 2049},
        {**PRODUCT, "title": "\x00"},
        {**PRODUCT, "title": " "},
        {**PRODUCT, "url": "http://a.example"},
        {**PRODUCT, "price": "x", "currency": "JPY"},
        {**PRODUCT, "price": "1", "currency": "x"},
        {**PRODUCT, "condition": "x"},
        {**PRODUCT, "price": "1"},
        {**PRODUCT, "description": "\u3042" * 10_900},
    ]
    assert {outcome(1, "product", fields)[0] for fields in samples} == v1_codes
