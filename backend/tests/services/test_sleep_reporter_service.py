"""Unit tests for SleepReporterService identity/name resolution helpers.

Issue #1201: batch-resolve ``user_id → email`` so the Sleep report list can
distinguish same-named contexts that belong to different user partitions.
Mirrors the existing ``resolve_context_names`` batch pattern.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.sleep_reporter_service import SleepReporterService


class TestResolveUserLabels:
    """SleepReporterService.resolve_user_labels — batch user_id → email."""

    @pytest.mark.asyncio
    async def test_maps_user_ids_to_emails(self):
        db = AsyncMock()
        result = MagicMock()
        result.all.return_value = [
            ("user_a", "a@test.com"),
            ("user_b", "b@test.com"),
        ]
        db.execute.return_value = result

        svc = SleepReporterService(db)
        labels = await svc.resolve_user_labels({"user_a", "user_b"})

        assert labels == {"user_a": "a@test.com", "user_b": "b@test.com"}

    @pytest.mark.asyncio
    async def test_omits_unresolved_ids(self):
        """A connector/non-human user_id absent from ``users`` is omitted (not None),
        so the caller can fall back to a shortened id in the UI."""
        db = AsyncMock()
        result = MagicMock()
        result.all.return_value = [("human", "h@test.com")]  # 'connector_x' absent
        db.execute.return_value = result

        svc = SleepReporterService(db)
        labels = await svc.resolve_user_labels({"human", "connector_x"})

        assert labels == {"human": "h@test.com"}
        assert "connector_x" not in labels

    @pytest.mark.asyncio
    async def test_empty_input_skips_query(self):
        """No ids → no DB round-trip (mirrors resolve_context_names)."""
        db = AsyncMock()

        svc = SleepReporterService(db)
        labels = await svc.resolve_user_labels(set())

        assert labels == {}
        db.execute.assert_not_called()
