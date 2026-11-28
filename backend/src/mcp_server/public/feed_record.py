"""What a public result shows for an unedited feed record (#3032).

A feed (``models.sync_feed_schemas``) is indexed as one row per record whose
``content`` is the record's JSON and whose ``summary`` is a mechanical label.
For such a row the public projection shows the record's label (``_LABELS``:
the title of a product, the question of an FAQ, the title and heading of a
page section, the title or product name of a review), its URL and its kind
instead.

One definition, used by every public surface that shows a row: the row is the
indexer's (``source``), never edited (``version == 1``; the indexer writes a
new row for every new version of a document) and its content is a record of
the feed contract in shape — a JSON object of at most the contract's record
size, a known ``record_type``, only the kind's fields plus ``record_type`` and
``external_id``, the required fields present, every value a string. A row
reads no installation's stored schema: a kind's fields are the union over
every registered schema version (``registered_kinds``). Any other row, and a
row whose label is empty, keeps its stored values. Every value shown comes from the row's own
``content``, which ``reference`` already returns for that row.

``feed_content_column`` is the SQL twin of the row gate for statements that do
not load the row; its value goes through ``parse_feed_record`` as well.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import and_, case, func
from sqlalchemy.sql.elements import Label

from models.memory import Memory
from models.sync_feed_schemas import MAX_RECORD_BYTES, registered_kinds
from services.guardrail_digest import flatten_summary

# The indexer's ``Memory.source`` (``services.resource_indexer.INDEXER_SOURCE``;
# a test pins the two equal).
FEED_RECORD_SOURCE = "resource_ingest"
FEED_RECORD_MAX_BYTES = MAX_RECORD_BYTES
# The contract's URL limit for a record field.
_URL_MAX_CHARS = 2_048
# Keys every record carries besides its kind's fields.
_RECORD_KEYS = frozenset({"record_type", "external_id"})


def _page_label(data: dict[str, str]) -> str | None:
    """A page section: ``title \u2014 heading``, or the title alone."""
    title = flatten_summary(data["title"])
    heading = flatten_summary(data.get("heading", ""))
    return f"{title} \u2014 {heading}" if title and heading else title


def _review_label(data: dict[str, str]) -> str | None:
    """A review: its title, else ``Review of <product name>``, else none (never its body)."""
    title = flatten_summary(data.get("title", ""))
    if title:
        return title
    product = flatten_summary(data.get("product_name", ""))
    return f"Review of {product}" if product else None


# kind -> the label of a record of that kind (its fields are strings), or None.
_LABELS: dict[str, Callable[[dict[str, str]], str | None]] = {
    "product": lambda data: data["title"],
    "faq": lambda data: data["question"],
    "page": _page_label,
    "review": _review_label,
}
# kind -> (allowed fields, required fields, label). A registered kind without a
# label function fails at import.
_KINDS: dict[str, tuple[frozenset[str], frozenset[str], Callable[[dict[str, str]], str | None]]] = {
    kind: (allowed, required, _LABELS[kind])
    for kind, (allowed, required) in registered_kinds().items()
}


def public_source_url(uri: str | None) -> str | None:
    """``uri`` when it is an absolute http(s) URL, else ``None`` (the #2675 rule)."""
    if not uri:
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme.lower() in ("http", "https") and parts.netloc:
        return uri
    return None


@dataclass(frozen=True)
class FeedRecord:
    """The public face of one feed record: its kind, label and URL (or ``None``)."""

    kind: str
    label: str
    url: str | None


def _record_url(value: str | None) -> str | None:
    """The record's URL when it passes the contract's URL rule, else ``None``."""
    if value is None or len(value) > _URL_MAX_CHARS or public_source_url(value) is None:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or any(c.isspace() for c in value)
    ):
        return None
    return value


def parse_feed_record(content: str | None) -> FeedRecord | None:
    """The record ``content`` holds, or ``None`` when it is not a feed record in shape."""
    if not isinstance(content, str) or len(content) > FEED_RECORD_MAX_BYTES:
        return None
    try:
        if len(content.encode()) > FEED_RECORD_MAX_BYTES:
            return None
        data: Any = json.loads(content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("record_type")
    if not isinstance(kind, str) or kind not in _KINDS:
        return None
    allowed, required, label_of = _KINDS[kind]
    keys = set(data)
    if not keys <= allowed | _RECORD_KEYS or not (required | _RECORD_KEYS) <= keys:
        return None
    if not all(isinstance(value, str) for value in data.values()):
        return None
    label = flatten_summary(label_of(data) or "")
    if not label:
        return None
    return FeedRecord(kind=kind, label=label, url=_record_url(data.get("url")))


def feed_record_of(memory: Memory) -> FeedRecord | None:
    """The feed record of a loaded row, or ``None`` for any other row."""
    if memory.source != FEED_RECORD_SOURCE or memory.version != 1:
        return None
    return parse_feed_record(memory.content)


def feed_content_column() -> Label[Any]:
    """``memories.content`` of a row the gate admits, else NULL (``feed_content``).

    The caller runs ``parse_feed_record`` on the value, which decides; the
    size gate also bounds what a statement that selects it reads.
    """
    return case(
        (
            and_(
                Memory.source == FEED_RECORD_SOURCE,
                Memory.version == 1,
                func.octet_length(Memory.content) <= FEED_RECORD_MAX_BYTES,
            ),
            Memory.content,
        ),
    ).label("feed_content")
