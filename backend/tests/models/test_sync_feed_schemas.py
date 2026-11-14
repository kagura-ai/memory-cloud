"""Public feed validation and content identities, independent of persistence."""

import pytest
from pydantic import ValidationError

from models.sync_feed_schemas import FeedRecord, destructive, snapshot_digest


@pytest.mark.parametrize("value", ["", " spaced", "trailing ", "a\x00b", "e\u0301", "a" * 201])
def test_ambiguous_external_identity_is_rejected(value):
    with pytest.raises(ValidationError):
        FeedRecord(external_id=value, fields={})


@pytest.mark.parametrize(
    "fields",
    [
        {"question": "Q", "answer": "A", "private": "hidden"},
        {"question": "Q", "answer": "A", "url": "https://user:pass@example.test"},
        {"question": "Q", "answer": "A" * 16001},
    ],
)
def test_unapproved_or_unbounded_faq_fields_are_rejected(fields):
    with pytest.raises(ValueError):
        FeedRecord(external_id="faq", fields=fields).public_payload("faq")


def test_snapshot_digest_ignores_chunk_order_but_not_content():
    records = [
        {"external_id": "b", "fields": {"title": "B"}},
        {"external_id": "a", "fields": {"title": "A"}},
    ]
    assert snapshot_digest(records) == snapshot_digest(list(reversed(records)))
    assert snapshot_digest(records) != snapshot_digest(records[:1])


@pytest.mark.parametrize(
    "deleted,previous,expected",
    [
        (0, 0, False),
        (1, 1, True),
        (1, 5, True),
        (1, 6, False),
        (100, 10000, True),
        (99, 10000, False),
    ],
)
def test_destructive_threshold_boundaries(deleted, previous, expected):
    assert destructive([{"op": "delete"}] * deleted, previous) is expected
