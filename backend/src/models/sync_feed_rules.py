"""Value rules of the feed record schema versions, with one reason per refusal (#3055).

The rules are frozen with the schema versions: senders build against them and
stored rows are validated again on every read, so a rule is never tightened.
``check_record`` refuses a record with the first failing rule, in this order:

1. ``unknown_field``: each key of ``fields``, in the record's key order, must be
   allowed for the kind;
2. ``missing_field``: each required key of the kind, in code-point order;
3. each value, in the record's key order: its length, its characters, blank,
   then its format (``FieldRule``);
4. the rules across fields (prices, then ratings);
5. ``record_too_large``: the record's JSON above ``MAX_RECORD_BYTES``.

The server never trims or normalizes a value: it stores it as sent or refuses
the record. A character is a code point. Every version-1 field keeps its
version-1 character rule in both versions; the new fields have stricter ones.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

# A record's JSON (``check_record``'s payload) is at most this many UTF-8 bytes.
MAX_RECORD_BYTES = 32_768

# ---------------------------------------------------------------------------
# Field names per version and kind
# ---------------------------------------------------------------------------
PRODUCT = {
    "title",
    "url",
    "description",
    "image_url",
    "price",
    "currency",
    "availability",
    "brand",
    "sku",
    "gtin",
    "mpn",
    "condition",
}
FAQ = {"question", "answer", "url"}
PRODUCT_REQUIRED = {"title", "url"}
FAQ_REQUIRED = {"question", "answer"}
RECORD_KEYS = frozenset({"record_type", "external_id"})
FIELDS = PRODUCT | FAQ | RECORD_KEYS
INDEXED = frozenset({"title", "description", "question", "answer", "brand", "sku"})

PRODUCT_V2 = frozenset(
    PRODUCT
    | {
        "availability_note",
        "product_highlights",
        "product_types",
        "product_details",
        "color",
        "size",
        "material",
        "sale_price",
        "sale_price_effective_date",
        "attributes",
    }
)
PAGE_V2 = frozenset({"title", "url", "body", "heading"})
PAGE_V2_REQUIRED = frozenset({"title", "url", "body"})
REVIEW_V2 = frozenset(
    {
        "title",
        "body",
        "url",
        "rating",
        "rating_min",
        "rating_max",
        "reviewed_at",
        "product_name",
        "product_url",
        "gtin",
        "brand",
        "sku",
        "mpn",
        "incentivized",
        "verified_purchase",
    }
)
REVIEW_V2_REQUIRED = frozenset({"body"})
FIELDS_V2 = frozenset(PRODUCT_V2 | FAQ | PAGE_V2 | REVIEW_V2 | RECORD_KEYS)
INDEXED_V2 = INDEXED | {
    "product_highlights",
    "product_types",
    "product_details",
    "color",
    "size",
    "material",
    "body",
    "heading",
    "product_name",
}
# ``attributes`` keys that are never allowed: version 2's 36 definition names
# (frozen with version 2; a later version does not change this list).
RESERVED_ATTRIBUTE_KEYS = FIELDS_V2

# ---------------------------------------------------------------------------
# Characters
# ---------------------------------------------------------------------------
# Unicode general category Cc (fixed by Unicode's stability policy) plus the
# line and paragraph separators.
CONTROL = frozenset(
    [chr(c) for c in range(0x00, 0x20)] + [chr(c) for c in range(0x7F, 0xA0)] + ["\u2028", "\u2029"]
)
# The Unicode White_Space property (25 code points). Format characters such as
# U+200B and U+FEFF are not whitespace.
WHITESPACE = frozenset(
    [chr(c) for c in range(0x09, 0x0E)]
    + [" ", "\u0085", "\u00a0", "\u1680"]
    + [chr(c) for c in range(0x2000, 0x200B)]
    + ["\u2028", "\u2029", "\u202f", "\u205f", "\u3000"]
)

PRICE = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?")
CURRENCY = re.compile(r"[A-Z]{3}")
RATING = re.compile(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?")
DATE_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,6}))?(Z|[+-][0-9]{2}:[0-9]{2})"
)
ATTRIBUTE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
# The published limits of a version-2 ``attributes`` value (``/schema``).
ATTRIBUTES_LIMITS = {
    "bytes": 8192,
    "keys": 50,
    "key_chars": 64,
    "values_per_key": 20,
    "value_chars": 2048,
}

REASONS = (
    "unknown_field",
    "missing_field",
    "too_long",
    "control_character",
    "blank",
    "invalid_url",
    "invalid_price",
    "invalid_currency",
    "invalid_enum",
    "invalid_date_time",
    "invalid_interval",
    "interval_order",
    "invalid_rating",
    "empty_line",
    "line_whitespace",
    "attributes_too_large",
    "attributes_invalid_json",
    "attributes_duplicate_key",
    "attributes_not_object",
    "attributes_empty",
    "attributes_too_many_keys",
    "attributes_invalid_key",
    "attributes_reserved_key",
    "attributes_value_not_list",
    "attributes_value_count",
    "attributes_value_not_string",
    "attributes_value_too_long",
    "attributes_value_control",
    "attributes_value_blank",
    "attributes_value_untrimmed",
    "attributes_value_not_nfc",
    "attributes_duplicate_value",
    "attributes_not_canonical",
    "requires_field",
    "invalid_rating_range",
    "rating_out_of_range",
    "record_too_large",
)
RefusalReason = Literal[
    "unknown_field",
    "missing_field",
    "too_long",
    "control_character",
    "blank",
    "invalid_url",
    "invalid_price",
    "invalid_currency",
    "invalid_enum",
    "invalid_date_time",
    "invalid_interval",
    "interval_order",
    "invalid_rating",
    "empty_line",
    "line_whitespace",
    "attributes_too_large",
    "attributes_invalid_json",
    "attributes_duplicate_key",
    "attributes_not_object",
    "attributes_empty",
    "attributes_too_many_keys",
    "attributes_invalid_key",
    "attributes_reserved_key",
    "attributes_value_not_list",
    "attributes_value_count",
    "attributes_value_not_string",
    "attributes_value_too_long",
    "attributes_value_control",
    "attributes_value_blank",
    "attributes_value_untrimmed",
    "attributes_value_not_nfc",
    "attributes_duplicate_value",
    "attributes_not_canonical",
    "requires_field",
    "invalid_rating_range",
    "rating_out_of_range",
    "record_too_large",
]


class RecordRefusal(ValueError):
    """One refused record: the first failing rule and the field it concerns (or ``None``)."""

    def __init__(self, reason: RefusalReason, field: str | None = None) -> None:
        super().__init__(reason)
        self.reason: RefusalReason = reason
        self.field = field


# ---------------------------------------------------------------------------
# Per-field rules
# ---------------------------------------------------------------------------
# ``v1``: no code point below U+0020 except tab, LF and CR (every version-1
# field, in both versions); ``one_line``: no CONTROL; ``lines``: no CONTROL
# except LF; ``text``: no CONTROL except tab, LF and CR.
CharClass = Literal["v1", "one_line", "lines", "text"]
Form = Literal[
    "none", "url", "price", "currency", "enum", "date_time", "interval", "rating", "lines",
    "attributes",
]  # fmt: skip


@dataclass(frozen=True)
class FieldRule:
    """The character limit, character class and format of one field."""

    limit: int
    chars: CharClass
    form: Form = "none"
    choices: frozenset[str] = frozenset()


def _rules_v1() -> dict[str, FieldRule]:
    rules = {name: FieldRule(2048, "v1") for name in PRODUCT | FAQ}
    rules.update(
        {
            "description": FieldRule(16_000, "v1"),
            "answer": FieldRule(16_000, "v1"),
            "url": FieldRule(2048, "v1", "url"),
            "image_url": FieldRule(2048, "v1", "url"),
            "price": FieldRule(2048, "v1", "price"),
            "currency": FieldRule(2048, "v1", "currency"),
            "availability": FieldRule(
                2048,
                "v1",
                "enum",
                frozenset({"in_stock", "out_of_stock", "preorder", "backorder"}),
            ),
            "condition": FieldRule(2048, "v1", "enum", frozenset({"new", "used", "refurbished"})),
        }
    )
    return dict(sorted(rules.items()))


_FLAG = frozenset({"true", "false"})
RULES_V1: Mapping[str, FieldRule] = _rules_v1()
RULES_V2: Mapping[str, FieldRule] = dict(
    sorted(
        {
            **RULES_V1,
            "availability_note": FieldRule(200, "one_line"),
            "color": FieldRule(2048, "one_line"),
            "size": FieldRule(2048, "one_line"),
            "material": FieldRule(2048, "one_line"),
            "product_highlights": FieldRule(2048, "lines", "lines"),
            "product_types": FieldRule(2048, "lines", "lines"),
            "product_details": FieldRule(2048, "lines", "lines"),
            "sale_price": FieldRule(2048, "one_line", "price"),
            "sale_price_effective_date": FieldRule(80, "one_line", "interval"),
            "attributes": FieldRule(ATTRIBUTES_LIMITS["bytes"], "one_line", "attributes"),
            "body": FieldRule(16_000, "text"),
            "heading": FieldRule(2048, "one_line"),
            "product_name": FieldRule(2048, "text"),
            "product_url": FieldRule(2048, "one_line", "url"),
            "rating": FieldRule(2048, "one_line", "rating"),
            "rating_min": FieldRule(2048, "one_line", "rating"),
            "rating_max": FieldRule(2048, "one_line", "rating"),
            "reviewed_at": FieldRule(40, "one_line", "date_time"),
            "incentivized": FieldRule(2048, "one_line", "enum", _FLAG),
            "verified_purchase": FieldRule(2048, "one_line", "enum", _FLAG),
        }.items()
    )
)


def _bad_character(chars: CharClass, value: str) -> bool:
    if chars == "v1":
        return any(ord(c) < 32 and c not in "\n\r\t" for c in value)
    if chars == "text":
        return any(c in CONTROL and c not in "\n\r\t" for c in value)
    if chars == "lines":
        return any(c in CONTROL and c != "\n" for c in value)
    return any(c in CONTROL for c in value)


def _blank(value: str) -> bool:
    return all(c in WHITESPACE for c in value)


def parse_date_time(value: str) -> datetime:
    """One date-time of the fixed RFC 3339 profile; raises ``ValueError``.

    Years 0001-9999, a valid day of the month (proleptic Gregorian), hour
    00-23, minute and second 00-59 (no leap second), 1-6 fraction digits, zone
    ``Z`` or ``+HH:MM`` / ``-HH:MM`` with HH 00-23 and MM 00-59, ``-00:00``
    refused. Uppercase ``T`` and ``Z`` only.
    """
    match = DATE_TIME.fullmatch(value)
    if match is None:
        raise ValueError("invalid_date_time")
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    if zone == "Z":
        tzinfo = UTC
    else:
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if hours > 23 or minutes > 59 or zone == "-00:00":
            raise ValueError("invalid_date_time")
        sign = 1 if zone[0] == "+" else -1
        tzinfo = timezone(sign * timedelta(hours=hours, minutes=minutes))
    micro = int(fraction.ljust(6, "0")) if fraction else 0
    # datetime() refuses year 0, month 13, 30 February, hour 24, minute 60, second 60.
    return datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=tzinfo
    )


class _DuplicateKey(ValueError):
    pass


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def check_attributes(text: str) -> None:
    """The structured ``attributes`` value: a canonical JSON object of string lists."""

    def refuse(reason: RefusalReason) -> RecordRefusal:
        return RecordRefusal(reason, "attributes")

    if len(text.encode("utf-8")) > ATTRIBUTES_LIMITS["bytes"]:
        raise refuse("attributes_too_large")
    try:
        value = json.loads(text, object_pairs_hook=_unique_pairs)
    except _DuplicateKey:
        raise refuse("attributes_duplicate_key") from None
    except (ValueError, RecursionError):
        raise refuse("attributes_invalid_json") from None
    if not isinstance(value, dict):
        raise refuse("attributes_not_object")
    if not value:
        raise refuse("attributes_empty")
    if len(value) > ATTRIBUTES_LIMITS["keys"]:
        raise refuse("attributes_too_many_keys")
    for key, items in value.items():
        if not ATTRIBUTE_KEY.fullmatch(key):
            raise refuse("attributes_invalid_key")
        if key in RESERVED_ATTRIBUTE_KEYS:
            raise refuse("attributes_reserved_key")
        if not isinstance(items, list):
            raise refuse("attributes_value_not_list")
        if not 1 <= len(items) <= ATTRIBUTES_LIMITS["values_per_key"]:
            raise refuse("attributes_value_count")
        for item in items:
            if not isinstance(item, str):
                raise refuse("attributes_value_not_string")
            if len(item) > ATTRIBUTES_LIMITS["value_chars"]:
                raise refuse("attributes_value_too_long")
            if any(c in CONTROL for c in item):
                raise refuse("attributes_value_control")
            if _blank(item):
                raise refuse("attributes_value_blank")
            if item[0] in WHITESPACE or item[-1] in WHITESPACE:
                raise refuse("attributes_value_untrimmed")
            if unicodedata.normalize("NFC", item) != item:
                raise refuse("attributes_value_not_nfc")
        if len(set(items)) != len(items):
            raise refuse("attributes_duplicate_value")
    if json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) != text:
        raise refuse("attributes_not_canonical")


def _url_ok(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not any(c.isspace() for c in value)
        )
    except ValueError:
        return False


def check_value(name: str, value: str, rule: FieldRule) -> None:
    """Length, characters, blank, then the field's format."""
    if len(value) > rule.limit:
        raise RecordRefusal("too_long", name)
    if _bad_character(rule.chars, value):
        raise RecordRefusal("control_character", name)
    if _blank(value):  # also the empty string
        raise RecordRefusal("blank", name)
    form = rule.form
    if form == "url":
        if not _url_ok(value):
            raise RecordRefusal("invalid_url", name)
    elif form == "price":
        if not PRICE.fullmatch(value):
            raise RecordRefusal("invalid_price", name)
    elif form == "currency":
        if not CURRENCY.fullmatch(value):
            raise RecordRefusal("invalid_currency", name)
    elif form == "enum":
        if value not in rule.choices:
            raise RecordRefusal("invalid_enum", name)
    elif form == "date_time":
        try:
            parse_date_time(value)
        except ValueError:
            raise RecordRefusal("invalid_date_time", name) from None
    elif form == "interval":
        if value.count("/") != 1:
            raise RecordRefusal("invalid_interval", name)
        start_text, end_text = value.split("/")
        try:
            start, end = parse_date_time(start_text), parse_date_time(end_text)
        except ValueError:
            raise RecordRefusal("invalid_date_time", name) from None
        if not start < end:  # instants: aware datetimes compare in UTC
            raise RecordRefusal("interval_order", name)
    elif form == "rating":
        if not RATING.fullmatch(value):
            raise RecordRefusal("invalid_rating", name)
    elif form == "lines":
        for line in value.split("\n"):
            if line == "":
                raise RecordRefusal("empty_line", name)
            if line[0] in WHITESPACE or line[-1] in WHITESPACE:
                raise RecordRefusal("line_whitespace", name)
    elif form == "attributes":
        check_attributes(value)


def _check_across(fields: Mapping[str, str]) -> None:
    present = fields.keys()
    if "price" in present and "currency" not in present:
        raise RecordRefusal("requires_field", "currency")
    if "currency" in present and "price" not in present:
        raise RecordRefusal("requires_field", "price")
    if "sale_price" in present and "price" not in present:
        raise RecordRefusal("requires_field", "price")
    if "sale_price_effective_date" in present and "sale_price" not in present:
        raise RecordRefusal("requires_field", "sale_price")
    group = ("rating", "rating_min", "rating_max")
    if any(name in present for name in group):
        for name in group:
            if name not in present:
                raise RecordRefusal("requires_field", name)
        rating, low, high = (Decimal(fields[name]) for name in group)
        if not low < high:
            raise RecordRefusal("invalid_rating_range", "rating_max")
        if not low <= rating <= high:
            raise RecordRefusal("rating_out_of_range", "rating")


def check_record(
    kind: str,
    external_id: str,
    fields: Mapping[str, str],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    rules: Mapping[str, FieldRule],
    names: frozenset[str],
) -> dict[str, str]:
    """The stored payload of one record of ``kind``, or :class:`RecordRefusal`.

    Args:
        kind: The feed kind; ``allowed`` and ``required`` are its keys.
        external_id: The record's identity (validated at parsing).
        fields: The record's ``fields`` in its key order, every value a string.
        allowed: Keys the kind's ``fields`` may hold.
        required: Keys the kind's ``fields`` must hold.
        rules: The version's rule per field key.
        names: The version's definition names; an unknown key is reported as
            ``field`` only when it is one of them.
    """
    for name in fields:
        if name not in allowed:
            raise RecordRefusal("unknown_field", name if name in names else None)
    for name in sorted(required):
        if name not in fields:
            raise RecordRefusal("missing_field", name)
    for name, value in fields.items():
        check_value(name, value, rules[name])
    _check_across(fields)
    payload = {"record_type": kind, "external_id": external_id, **fields}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_RECORD_BYTES:
        raise RecordRefusal("record_too_large")
    return payload
