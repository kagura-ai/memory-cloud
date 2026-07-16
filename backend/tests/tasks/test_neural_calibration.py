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
from tasks.neural_calibration import _delete_calibration, _upsert_calibration

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


@pytest.mark.asyncio
async def test_delete_calibration_issues_delete_without_commit():
    """#982: skipping an edge_gate upsert must delete any stale row (so the
    runtime falls back to the absolute config value), without committing —
    the caller owns the commit."""
    db = _mock_db()
    await _delete_calibration(
        db, "text-embedding-3-small", 512, None, kind=CALIBRATION_KIND_EDGE_GATE
    )
    db.execute.assert_awaited_once()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_compute_calibration_passes_vector_list_to_measure_random_pair(monkeypatch):
    """#1319: fetch_vectors returns dict[str, list[float]]; measure_random_pair
    expects a bare vector list. Passing the dict made np.asarray call
    float(dict) and every calibration run failed pre-commit (neither kind
    persisted). The edge_gate sampling must receive list(vectors.values())."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from uuid import uuid4

    import tasks.neural_calibration as nc

    sampled = [SimpleNamespace(id=uuid4()) for _ in range(3)]
    vectors_by_id = {str(m.id): [0.1 * (i + 1), 0.2] for i, m in enumerate(sampled)}
    captured: dict = {}

    def fake_measure_random_pair(vectors, n_pairs):
        captured["vectors"] = vectors
        return [0.2] * nc.EDGE_GATE_MIN_PAIR_OBSERVATIONS

    async def fake_sample_memories(db, ctx_id, n):
        return sampled

    async def fake_fetch_vectors(qdrant, collection, ids):
        return vectors_by_id

    async def fake_measure_top_k(mems, vectors, collection, k):
        assert vectors is vectors_by_id  # top-k keeps consuming the dict form
        return [0.5] * nc.BOOTSTRAP_MIN_OBSERVATIONS, nc.BOOTSTRAP_MIN_MEMORIES

    script = SimpleNamespace(
        compute_percentiles=lambda scores: dict(_PCTS),
        fetch_vectors=fake_fetch_vectors,
        measure_top_k=fake_measure_top_k,
        measure_random_pair=fake_measure_random_pair,
        sample_memories=fake_sample_memories,
    )
    monkeypatch.setattr(nc, "_load_measure_script", lambda: script)
    monkeypatch.setattr(nc, "get_qdrant_client", lambda: MagicMock())
    monkeypatch.setattr(
        nc.NeuralMemoryConfig,
        "from_db",
        AsyncMock(return_value=SimpleNamespace(calibration_ttl_days=30)),
    )
    upsert = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(nc, "_upsert_calibration", upsert)

    db = _mock_db()
    row = await nc.compute_calibration(db, "text-embedding-3-small", 512, uuid4())

    assert row is upsert.return_value
    assert isinstance(captured["vectors"], list)
    assert captured["vectors"] == list(vectors_by_id.values())
    assert upsert.await_count == 2  # knn_seed + edge_gate both staged
    db.commit.assert_awaited_once()
