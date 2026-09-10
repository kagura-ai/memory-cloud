"""``build_memory_point`` — the one definition of a memory as a Qdrant point.

Extracted from ``process_pending_embedding`` for the embedding-model migration
(#1525). The migration writes into a *different* collection and the whole
point of the dual-collection design is that the new collection is a drop-in
for the old one, so payload and sparse vector must come from the same code.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from services.memory_service import build_memory_point


def _memory(**overrides) -> MagicMock:
    memory = MagicMock()
    memory.user_id = "user-1"
    memory.summary = "Postgres JSONB GIN index speeds up tag queries"
    memory.context_summary = "Recall when tuning tag filters"
    memory.content = "Long body " * 300  # > 2000 chars once repeated
    memory.type = "pattern"
    memory.importance = 0.8
    memory.tags = ["postgres", "index"]
    memory.scope = "persistent"
    memory.client = "claude-code"
    memory.context = None
    memory.created_at = datetime(2026, 1, 2, 3, 4, 5)
    memory.updated_at = None
    memory.location_lat = None
    memory.location_lon = None
    for key, value in overrides.items():
        setattr(memory, key, value)
    return memory


class TestBuildMemoryPoint:
    def test_payload_carries_every_searchable_field(self):
        payload, indices, values = build_memory_point(_memory())
        for key in (
            "user_id",
            "summary",
            "context_summary",
            "summary_tokens",
            "context_summary_tokens",
            "content_tokens",
            "summary_reading",
            "type",
            "importance",
            "tags",
            "scope",
            "client",
            "created_at",
            "updated_at",
        ):
            assert key in payload, key
        assert payload["tags"] == ["postgres", "index"]
        assert payload["client"] == "claude-code"
        # Isolation fields are added by add_memory_to_qdrant, not here.
        assert "workspace_id" not in payload and "context_id" not in payload
        # A non-empty document yields a non-empty, aligned sparse vector.
        assert len(indices) == len(values) > 0

    def test_content_is_truncated_to_2000_chars_for_bm25(self):
        long_memory = _memory(content="x" * 5000)
        short_memory = _memory(content="x" * 2000)
        assert (
            build_memory_point(long_memory)[0]["content_tokens"]
            == build_memory_point(short_memory)[0]["content_tokens"]
        )

    def test_updated_at_falls_back_to_created_at(self):
        payload, _, _ = build_memory_point(_memory(updated_at=None))
        assert payload["updated_at"] == payload["created_at"]

    def test_optional_context_and_location(self):
        payload, _, _ = build_memory_point(_memory())
        assert "context" not in payload and "location" not in payload

        payload, _, _ = build_memory_point(
            _memory(context={"issue": 1}, location_lat=35.68, location_lon=139.77)
        )
        assert payload["context"] == {"issue": 1}
        assert payload["location"] == {"lat": 35.68, "lon": 139.77}

    @pytest.mark.parametrize("lat,lon", [(35.68, None), (None, 139.77)])
    def test_half_a_coordinate_pair_is_not_a_location(self, lat, lon):
        payload, _, _ = build_memory_point(_memory(location_lat=lat, location_lon=lon))
        assert "location" not in payload

    def test_missing_client_defaults_to_unknown(self):
        payload, _, _ = build_memory_point(_memory(client=None))
        assert payload["client"] == "unknown"
