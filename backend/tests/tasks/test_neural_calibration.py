"""Unit tests for the calibration upsert helper (#982).

``compute_calibration`` itself is integration-tested (it drives Qdrant + the
measurement script + DB); these cover the extracted ``_upsert_calibration``
helper in isolation — specifically the kind-scoping that keeps a knn_seed
recalibration from wiping the coexisting edge_gate row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.neural import (
    CALIBRATION_KIND_EDGE_GATE,
    CALIBRATION_KIND_KNN_SEED,
)
from tasks.neural_calibration import _upsert_calibration

_PCTS = {"p25": 0.10, "p50": 0.15, "p75": 0.20, "p90": 0.25, "p95": 0.30, "p99": 0.40}


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()  # sync method
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [CALIBRATION_KIND_KNN_SEED, CALIBRATION_KIND_EDGE_GATE])
async def test_upsert_builds_row_with_requested_kind(kind):
    db = _mock_db()
    now = datetime.now(UTC)
    row = await _upsert_calibration(
        db,
        "text-embedding-3-small",
        512,
        None,
        kind=kind,
        percentiles=_PCTS,
        observations=100,
        now=now,
        valid_until=now + timedelta(days=30),
    )
    # A (kind-scoped) delete is issued, then the fresh row is added.
    db.execute.assert_awaited_once()
    db.add.assert_called_once()
    assert row.kind == kind
    assert row.p95 == 0.30
    assert row.sample_size == 100
    assert row.model_name == "text-embedding-3-small"
    assert row.dimensions == 512


@pytest.mark.asyncio
async def test_upsert_does_not_commit():
    """Helper must NOT commit — the caller commits once so both kinds are atomic."""
    db = _mock_db()
    now = datetime.now(UTC)
    await _upsert_calibration(
        db,
        "m",
        512,
        None,
        kind=CALIBRATION_KIND_EDGE_GATE,
        percentiles=_PCTS,
        observations=50,
        now=now,
        valid_until=now + timedelta(days=30),
    )
    db.commit.assert_not_called()
