"""Coverage-focused tests for ``tasks.neural_calibration`` (#406 / #982).

Complements ``test_neural_calibration.py`` (which covers the extracted
``_upsert_calibration`` / ``_delete_calibration`` helpers in isolation) by
driving the uncovered branches of the public task surface:

- ``_dedup_key`` global vs per-context key shapes.
- ``enqueue_recalibration_dedup`` lock-acquired, lock-skipped, and
  Redis-error fail-open paths.
- ``_run_calibration`` happy / IntegrityError-race / generic-exception
  paths plus the always-release ``finally``.
- ``_release_dedup_lock`` empty-token skip, success, and swallowed error.
- ``_load_measure_script`` caching + ``sys.path`` restoration.
- ``_model_dims_where`` legacy-default vs explicit-config predicates.
- ``_pick_largest_context_for_model`` (real DB) found / not-found.
- ``compute_calibration`` (real DB) no-context, no-sample, D3-gate-fail,
  no-percentiles, happy-path-with-edge-gate, and edge-gate-skip branches.
- ``maybe_trigger_bootstrap`` throttle, no-context, below-gate, and
  enqueue paths.

External I/O (Redis, Qdrant, the measure script) is mocked; the DB via
``db_session`` is real. ``asyncio_mode=auto`` so async tests need no marker.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import tasks.neural_calibration as nc
from models.auth import Context, Workspace
from models.config import ContextSearchConfig
from models.memory import Memory
from models.neural import (
    CALIBRATION_KIND_EDGE_GATE,
    CALIBRATION_KIND_KNN_SEED,
    EmbeddingCalibration,
)
from tasks.neural_calibration import (
    BOOTSTRAP_MIN_MEMORIES,
    _dedup_key,
    _delete_calibration,
    _load_measure_script,
    _model_dims_where,
    _pick_largest_context_for_model,
    _release_dedup_lock,
    _run_calibration,
    compute_calibration,
    enqueue_recalibration_dedup,
    maybe_trigger_bootstrap,
)

_PCTS = {"p25": 0.10, "p50": 0.15, "p75": 0.20, "p90": 0.25, "p95": 0.30, "p99": 0.40}


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
async def _make_context_with_memories(
    db_session,
    *,
    n_memories: int,
    embedding_model: str | None = "text-embedding-3-small",
    embedding_dimensions: int = 512,
    embedding_status: str = "success",
    add_search_config: bool = True,
) -> tuple[Workspace, Context]:
    """Create a Workspace + Context (+ optional search config) with N memories."""
    ws = Workspace(
        id=uuid4(),
        name=f"calib-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id="calib_user",
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db_session.add(ws)
    await db_session.flush()

    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"calib-ctx-{uuid4().hex[:8]}",
        created_by="calib_user",
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()

    if add_search_config and embedding_model is not None:
        db_session.add(
            ContextSearchConfig(
                context_id=ctx.id,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
            )
        )
        await db_session.flush()

    for i in range(n_memories):
        db_session.add(
            Memory(
                id=uuid4(),
                user_id="calib_user",
                workspace_id=ws.id,
                context_id=ctx.id,
                summary=f"mem {i}",
                content=f"content {i}",
                type="note",
                client="test",
                embedding_status=embedding_status,
            )
        )
    await db_session.flush()
    return ws, ctx


def _measure_module(
    *,
    sampled: list,
    scores: list[float],
    effective: int,
    pair_scores: list[float],
    percentiles=None,
    pair_percentiles=None,
) -> SimpleNamespace:
    """Build a fake measure-script module with controllable outputs."""
    real_pcts = _PCTS if percentiles is None else percentiles
    real_pair_pcts = _PCTS if pair_percentiles is None else pair_percentiles

    def compute_percentiles(values):
        if not values:
            return {}
        # Route knn vs pair by identity of the list passed in.
        if values is scores:
            return dict(real_pcts)
        if values is pair_scores:
            return dict(real_pair_pcts)
        return dict(_PCTS)

    return SimpleNamespace(
        compute_percentiles=compute_percentiles,
        sample_memories=AsyncMock(return_value=sampled),
        fetch_vectors=AsyncMock(return_value={"vec": [0.1]}),
        measure_top_k=AsyncMock(return_value=(scores, effective)),
        measure_random_pair=MagicMock(return_value=pair_scores),
    )


# --------------------------------------------------------------------------- #
# _dedup_key
# --------------------------------------------------------------------------- #
class TestDedupKey:
    """Key shape is unique per (model, dims, context)."""

    def test_global_uses_global_token(self):
        key = _dedup_key("text-embedding-3-small", 512, None)
        assert key == "neural:calibrate:text-embedding-3-small:512:global"

    def test_per_context_uses_uuid(self):
        cid = uuid4()
        key = _dedup_key("m", 1024, cid)
        assert key == f"neural:calibrate:m:1024:{cid}"
        assert key != _dedup_key("m", 1024, None)


# --------------------------------------------------------------------------- #
# _delete_calibration — per-context (context_id IS NOT NULL) branch
# --------------------------------------------------------------------------- #
class TestDeleteCalibrationPerContext:
    """The ``context_id != None`` DELETE branch (#982 kind-scoped per-context)."""

    async def test_per_context_delete_only_removes_that_context_row(self, db_session):
        model = f"perctx-{uuid4().hex[:8]}"
        # Real Context rows — embedding_calibrations.context_id has a FK.
        _, ctx_a = await _make_context_with_memories(
            db_session, n_memories=0, embedding_model=model, embedding_dimensions=512
        )
        _, ctx_b = await _make_context_with_memories(
            db_session, n_memories=0, embedding_model=model, embedding_dimensions=512
        )
        ctx_id = ctx_a.id
        other_ctx_id = ctx_b.id
        now = datetime.now(UTC)

        def _row(cid):
            return EmbeddingCalibration(
                model_name=model,
                dimensions=512,
                context_id=cid,
                kind=CALIBRATION_KIND_KNN_SEED,
                p25=0.1,
                p50=0.1,
                p75=0.1,
                p90=0.1,
                p95=0.1,
                p99=0.1,
                sample_size=100,
                sampled_at=now,
                valid_until=now + timedelta(days=30),
            )

        db_session.add_all([_row(ctx_id), _row(other_ctx_id)])
        await db_session.flush()

        # Delete only ctx_id's row via the context_id == ctx_id branch.
        await _delete_calibration(db_session, model, 512, ctx_id, kind=CALIBRATION_KIND_KNN_SEED)
        await db_session.flush()

        remaining = (
            (
                await db_session.execute(
                    select(EmbeddingCalibration).where(EmbeddingCalibration.model_name == model)
                )
            )
            .scalars()
            .all()
        )
        # The sibling context's row survives; only ctx_id's was removed.
        assert len(remaining) == 1
        assert remaining[0].context_id == other_ctx_id


# --------------------------------------------------------------------------- #
# enqueue_recalibration_dedup
# --------------------------------------------------------------------------- #
class TestEnqueueRecalibrationDedup:
    """Lock-acquired, lock-skipped, and Redis fail-open paths."""

    async def test_lock_acquired_spawns_task(self, monkeypatch):
        """SETNX succeeds -> task spawned, tracked, returns True."""
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        monkeypatch.setattr(nc, "get_redis_client", lambda: redis)

        spawned: dict = {}

        async def fake_run(model, dims, ctx, *, dedup_key, token):
            spawned["called"] = (model, dims, ctx, dedup_key, token)

        monkeypatch.setattr(nc, "_run_calibration", fake_run)

        result = await enqueue_recalibration_dedup("m", 512, None)
        assert result is True
        # set() called with nx=True and a TTL.
        _, kwargs = redis.set.call_args
        assert kwargs["nx"] is True
        assert kwargs["ex"] == nc._DEDUP_LOCK_TTL_SEC
        # Let the spawned task run.
        await asyncio.sleep(0)
        assert spawned["called"][0] == "m"
        assert spawned["called"][4] != ""  # a real token was written

    async def test_lock_not_acquired_returns_false(self, monkeypatch):
        """SETNX returns falsy -> duplicate dropped, no task."""
        redis = MagicMock()
        redis.set = AsyncMock(return_value=None)
        monkeypatch.setattr(nc, "get_redis_client", lambda: redis)
        called = MagicMock()
        monkeypatch.setattr(nc, "_run_calibration", AsyncMock(side_effect=called))

        result = await enqueue_recalibration_dedup("m", 512, None)
        assert result is False
        called.assert_not_called()

    async def test_redis_error_fail_open(self, monkeypatch):
        """Redis raising -> run anyway with empty token (fail-open)."""

        def boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr(nc, "get_redis_client", boom)
        spawned: dict = {}

        async def fake_run(model, dims, ctx, *, dedup_key, token):
            spawned["token"] = token

        monkeypatch.setattr(nc, "_run_calibration", fake_run)

        result = await enqueue_recalibration_dedup("m", 512, None)
        assert result is True
        await asyncio.sleep(0)
        # Fail-open clears the token so release skips the DEL.
        assert spawned["token"] == ""


# --------------------------------------------------------------------------- #
# _run_calibration
# --------------------------------------------------------------------------- #
class TestRunCalibration:
    """Happy / race / failure all release the lock."""

    async def test_happy_path_computes_then_releases(self, monkeypatch):
        mock_db = AsyncMock()

        async def fake_get_db():
            yield mock_db

        monkeypatch.setattr(nc, "get_db", fake_get_db)
        compute = AsyncMock()
        monkeypatch.setattr(nc, "compute_calibration", compute)
        release = AsyncMock()
        monkeypatch.setattr(nc, "_release_dedup_lock", release)

        await _run_calibration("m", 512, None, dedup_key="k", token="tok")

        compute.assert_awaited_once_with(mock_db, "m", 512, None)
        release.assert_awaited_once_with("k", "tok")

    async def test_integrity_error_swallowed_and_released(self, monkeypatch):
        """A partial-unique collision is logged at info and still releases."""

        async def fake_get_db():
            yield AsyncMock()

        monkeypatch.setattr(nc, "get_db", fake_get_db)
        monkeypatch.setattr(
            nc,
            "compute_calibration",
            AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup"))),
        )
        release = AsyncMock()
        monkeypatch.setattr(nc, "_release_dedup_lock", release)

        # Must not raise.
        await _run_calibration("m", 512, uuid4(), dedup_key="k", token="tok")
        release.assert_awaited_once()

    async def test_generic_exception_swallowed_and_released(self, monkeypatch):
        async def fake_get_db():
            yield AsyncMock()

        monkeypatch.setattr(nc, "get_db", fake_get_db)
        monkeypatch.setattr(nc, "compute_calibration", AsyncMock(side_effect=ValueError("boom")))
        release = AsyncMock()
        monkeypatch.setattr(nc, "_release_dedup_lock", release)

        await _run_calibration("m", 512, None, dedup_key="k", token="tok")
        release.assert_awaited_once_with("k", "tok")


# --------------------------------------------------------------------------- #
# _release_dedup_lock
# --------------------------------------------------------------------------- #
class TestReleaseDedupLock:
    """Empty-token skip, success, swallowed error."""

    async def test_empty_token_skips_redis(self, monkeypatch):
        called = MagicMock()
        monkeypatch.setattr(nc, "get_redis_client", called)
        await _release_dedup_lock("k", "")
        called.assert_not_called()

    async def test_success_runs_compare_and_delete(self, monkeypatch):
        script = AsyncMock(return_value=1)
        redis = MagicMock()
        redis.register_script = MagicMock(return_value=script)
        monkeypatch.setattr(nc, "get_redis_client", lambda: redis)

        await _release_dedup_lock("mykey", "mytoken")

        redis.register_script.assert_called_once_with(nc._DEDUP_RELEASE_SCRIPT)
        script.assert_awaited_once_with(keys=["mykey"], args=["mytoken"])

    async def test_redis_error_swallowed(self, monkeypatch):
        def boom():
            raise RuntimeError("redis exploded")

        monkeypatch.setattr(nc, "get_redis_client", boom)
        # Must not raise — TTL guarantees eventual release.
        await _release_dedup_lock("k", "tok")


# --------------------------------------------------------------------------- #
# _load_measure_script
# --------------------------------------------------------------------------- #
class TestLoadMeasureScript:
    """Caching + sys.path restoration."""

    def test_loads_and_exposes_expected_callables(self):
        mod = _load_measure_script()
        for name in (
            "compute_percentiles",
            "fetch_vectors",
            "measure_top_k",
            "measure_random_pair",
            "sample_memories",
        ):
            assert callable(getattr(mod, name))

    def test_result_is_cached(self):
        assert _load_measure_script() is _load_measure_script()

    def test_sys_path_restored(self):
        before = list(sys.path)
        _load_measure_script()
        assert sys.path == before

    def test_none_spec_raises_runtime_error(self, monkeypatch):
        """A None import spec -> RuntimeError (calibration cannot run)."""
        import importlib.util as _ilu

        # The result is @cache-memoized; clear it so the patched
        # spec_from_file_location is actually exercised, then clear again so
        # later tests get a fresh (real) load.
        _load_measure_script.cache_clear()
        try:
            monkeypatch.setattr(_ilu, "spec_from_file_location", lambda *a, **k: None)
            with pytest.raises(RuntimeError, match="unable to build import spec"):
                _load_measure_script()
        finally:
            _load_measure_script.cache_clear()


# --------------------------------------------------------------------------- #
# _model_dims_where
# --------------------------------------------------------------------------- #
class TestModelDimsWhere:
    """Legacy-default OR-clause vs explicit AND-clause."""

    async def test_legacy_default_matches_null_config(self, db_session):
        """text-embedding-3-small/512 contexts with NO search config still match."""
        _, ctx = await _make_context_with_memories(
            db_session, n_memories=3, add_search_config=False
        )
        picked = await _pick_largest_context_for_model(db_session, "text-embedding-3-small", 512)
        assert picked is not None
        assert picked[0] == ctx.id
        assert picked[1] == 3

    async def test_non_legacy_requires_explicit_config(self, db_session):
        """A non-default model only matches a context with the matching config."""
        _, ctx = await _make_context_with_memories(
            db_session,
            n_memories=4,
            embedding_model="voyage-3",
            embedding_dimensions=1024,
        )
        picked = await _pick_largest_context_for_model(db_session, "voyage-3", 1024)
        assert picked is not None
        assert picked[0] == ctx.id
        assert picked[1] == 4

    async def test_non_legacy_without_config_does_not_match(self, db_session):
        """No config row -> a non-legacy (model, dims) finds nothing for it."""
        await _make_context_with_memories(
            db_session,
            n_memories=4,
            embedding_model=None,
            add_search_config=False,
        )
        picked = await _pick_largest_context_for_model(db_session, "some-other-model", 4096)
        assert picked is None

    def test_legacy_default_predicate_includes_null_config_fallback(self):
        """The legacy default predicate OR-s in a NULL-config clause so that
        pre-routing contexts (no ContextSearchConfig row) are matched."""
        clause = _model_dims_where("text-embedding-3-small", 512)
        sql = str(clause.compile(compile_kwargs={"literal_binds": True}))
        assert "IS NULL" in sql.upper()  # the NULL-config fallback branch
        assert "text-embedding-3-small" in sql
        assert "512" in sql

    def test_non_legacy_predicate_has_no_null_fallback(self):
        """A non-default (model, dims) is a plain AND with no NULL-config
        fallback — otherwise it would wrongly sweep in legacy contexts."""
        clause = _model_dims_where("voyage-3", 1024)
        sql = str(clause.compile(compile_kwargs={"literal_binds": True}))
        assert "IS NULL" not in sql.upper()
        assert "voyage-3" in sql
        assert "1024" in sql


# --------------------------------------------------------------------------- #
# _pick_largest_context_for_model
# --------------------------------------------------------------------------- #
class TestPickLargestContextForModel:
    """Largest-context selection and the empty case."""

    async def test_returns_none_when_no_memories(self, db_session):
        picked = await _pick_largest_context_for_model(
            db_session, f"absent-model-{uuid4().hex}", 333
        )
        assert picked is None

    async def test_picks_largest_and_ignores_deleted_and_pending(self, db_session):
        model = f"pick-model-{uuid4().hex[:8]}"
        # Small context: 2 memories.
        await _make_context_with_memories(
            db_session, n_memories=2, embedding_model=model, embedding_dimensions=512
        )
        # Large context: 5 success memories + noise (deleted / pending).
        _, big = await _make_context_with_memories(
            db_session, n_memories=5, embedding_model=model, embedding_dimensions=512
        )
        # A deleted + a pending memory in the big context must NOT count.
        db_session.add(
            Memory(
                id=uuid4(),
                user_id="calib_user",
                workspace_id=big.workspace_id,
                context_id=big.id,
                summary="deleted",
                content="x",
                type="note",
                client="test",
                embedding_status="success",
                deleted_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        db_session.add(
            Memory(
                id=uuid4(),
                user_id="calib_user",
                workspace_id=big.workspace_id,
                context_id=big.id,
                summary="pending",
                content="x",
                type="note",
                client="test",
                embedding_status="pending",
            )
        )
        await db_session.flush()

        picked = await _pick_largest_context_for_model(db_session, model, 512)
        assert picked is not None
        assert picked[0] == big.id
        assert picked[1] == 5  # deleted + pending excluded


# --------------------------------------------------------------------------- #
# compute_calibration  (real DB; measure-script + qdrant mocked)
# --------------------------------------------------------------------------- #
class TestComputeCalibration:
    """Every return branch of the compute path."""

    async def test_no_context_for_model_returns_none(self, db_session, monkeypatch):
        """Global calibration with no context for the (model, dims) -> None."""
        monkeypatch.setattr(nc, "get_qdrant_client", MagicMock())
        result = await compute_calibration(db_session, f"no-ctx-{uuid4().hex}", 512, None)
        assert result is None

    async def test_no_memories_sampled_returns_none(self, db_session, monkeypatch):
        model = f"empty-sample-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session, n_memories=3, embedding_model=model, embedding_dimensions=512
        )
        fake = _measure_module(sampled=[], scores=[], effective=0, pair_scores=[])
        monkeypatch.setattr(nc, "_load_measure_script", lambda: fake)
        monkeypatch.setattr(nc, "get_qdrant_client", MagicMock())

        result = await compute_calibration(db_session, model, 512, None)
        assert result is None

    async def test_d3_gate_failure_returns_none(self, db_session, monkeypatch):
        """Too few effective memories AND too few observations -> no row written."""
        model = f"d3fail-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session, n_memories=3, embedding_model=model, embedding_dimensions=512
        )
        sampled = [SimpleNamespace(id=uuid4())]
        fake = _measure_module(
            sampled=sampled,
            scores=[0.1, 0.2, 0.3],  # 3 observations < 10000
            effective=5,  # < 200
            pair_scores=[],
        )
        monkeypatch.setattr(nc, "_load_measure_script", lambda: fake)
        monkeypatch.setattr(nc, "get_qdrant_client", MagicMock())

        result = await compute_calibration(db_session, model, 512, None)
        assert result is None
        # No row persisted.
        rows = (
            (
                await db_session.execute(
                    select(EmbeddingCalibration).where(EmbeddingCalibration.model_name == model)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    async def test_no_percentiles_returns_none(self, db_session, monkeypatch):
        """D3 passes but compute_percentiles returns empty -> None."""
        model = f"nopct-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session, n_memories=3, embedding_model=model, embedding_dimensions=512
        )
        sampled = [SimpleNamespace(id=uuid4())]
        scores = [0.5] * 250  # observations >= 200-equivalent, effective high
        fake = _measure_module(
            sampled=sampled,
            scores=scores,
            effective=250,
            pair_scores=[],
            percentiles={},  # force the "no scores" branch
        )
        monkeypatch.setattr(nc, "_load_measure_script", lambda: fake)
        monkeypatch.setattr(nc, "get_qdrant_client", MagicMock())

        result = await compute_calibration(db_session, model, 512, None)
        assert result is None

    async def test_happy_path_writes_both_kinds(self, db_session, monkeypatch):
        """D3 passes with enough pairs -> knn_seed + edge_gate rows written."""
        model = f"happy-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session, n_memories=3, embedding_model=model, embedding_dimensions=512
        )
        sampled = [SimpleNamespace(id=uuid4())]
        scores = [0.4] * 250
        pair_scores = [0.1] * 50  # >= EDGE_GATE_MIN_PAIR_OBSERVATIONS (30)
        fake = _measure_module(
            sampled=sampled, scores=scores, effective=250, pair_scores=pair_scores
        )
        monkeypatch.setattr(nc, "_load_measure_script", lambda: fake)
        monkeypatch.setattr(nc, "get_qdrant_client", MagicMock())

        result = await compute_calibration(db_session, model, 512, None)
        assert result is not None
        assert result.kind == CALIBRATION_KIND_KNN_SEED
        assert result.sample_size == 250
        assert result.p90 == _PCTS["p90"]

        rows = (
            (
                await db_session.execute(
                    select(EmbeddingCalibration)
                    .where(EmbeddingCalibration.model_name == model)
                    .order_by(EmbeddingCalibration.kind)
                )
            )
            .scalars()
            .all()
        )
        kinds = {r.kind for r in rows}
        assert kinds == {CALIBRATION_KIND_KNN_SEED, CALIBRATION_KIND_EDGE_GATE}
        edge = next(r for r in rows if r.kind == CALIBRATION_KIND_EDGE_GATE)
        assert edge.sample_size == 50

    async def test_edge_gate_skipped_deletes_stale_row(self, db_session, monkeypatch):
        """Too few random pairs -> edge_gate row is skipped AND any stale one deleted."""
        model = f"edgeskip-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session, n_memories=3, embedding_model=model, embedding_dimensions=512
        )
        # Seed a stale edge_gate row that must be deleted.
        now = datetime.now(UTC)
        db_session.add(
            EmbeddingCalibration(
                model_name=model,
                dimensions=512,
                context_id=None,
                kind=CALIBRATION_KIND_EDGE_GATE,
                p25=0.1,
                p50=0.1,
                p75=0.1,
                p90=0.1,
                p95=0.1,
                p99=0.1,
                sample_size=99,
                sampled_at=now,
                valid_until=now + timedelta(days=30),
            )
        )
        await db_session.flush()

        sampled = [SimpleNamespace(id=uuid4())]
        scores = [0.4] * 250
        pair_scores = [0.1] * 5  # < EDGE_GATE_MIN_PAIR_OBSERVATIONS (30)
        fake = _measure_module(
            sampled=sampled, scores=scores, effective=250, pair_scores=pair_scores
        )
        monkeypatch.setattr(nc, "_load_measure_script", lambda: fake)
        monkeypatch.setattr(nc, "get_qdrant_client", MagicMock())

        result = await compute_calibration(db_session, model, 512, None)
        assert result is not None  # knn_seed still written

        rows = (
            (
                await db_session.execute(
                    select(EmbeddingCalibration).where(EmbeddingCalibration.model_name == model)
                )
            )
            .scalars()
            .all()
        )
        kinds = {r.kind for r in rows}
        assert kinds == {CALIBRATION_KIND_KNN_SEED}  # edge_gate gone

    async def test_edge_gate_skipped_empty_pair_percentiles(self, db_session, monkeypatch):
        """Empty pair percentiles (no scores) also routes to the skip branch."""
        model = f"edgeempty-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session, n_memories=3, embedding_model=model, embedding_dimensions=512
        )
        sampled = [SimpleNamespace(id=uuid4())]
        scores = [0.4] * 250
        fake = _measure_module(
            sampled=sampled,
            scores=scores,
            effective=250,
            pair_scores=[],  # -> compute_percentiles returns {} -> skip
        )
        monkeypatch.setattr(nc, "_load_measure_script", lambda: fake)
        monkeypatch.setattr(nc, "get_qdrant_client", MagicMock())

        result = await compute_calibration(db_session, model, 512, None)
        assert result is not None
        rows = (
            (
                await db_session.execute(
                    select(EmbeddingCalibration).where(EmbeddingCalibration.model_name == model)
                )
            )
            .scalars()
            .all()
        )
        assert {r.kind for r in rows} == {CALIBRATION_KIND_KNN_SEED}

    async def test_observations_only_gate_passes(self, db_session, monkeypatch):
        """effective < 200 but observations >= 10k still passes the D3 gate."""
        model = f"obsgate-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session, n_memories=3, embedding_model=model, embedding_dimensions=512
        )
        sampled = [SimpleNamespace(id=uuid4())]
        scores = [0.4] * 10_000  # observations >= 10000 even though effective small
        fake = _measure_module(sampled=sampled, scores=scores, effective=5, pair_scores=[0.1] * 40)
        monkeypatch.setattr(nc, "_load_measure_script", lambda: fake)
        monkeypatch.setattr(nc, "get_qdrant_client", MagicMock())

        result = await compute_calibration(db_session, model, 512, None)
        assert result is not None
        assert result.sample_size == 10_000


# --------------------------------------------------------------------------- #
# maybe_trigger_bootstrap
# --------------------------------------------------------------------------- #
class TestMaybeTriggerBootstrap:
    """Throttle, no-context, below-gate, and enqueue branches."""

    @pytest.fixture(autouse=True)
    def _clear_throttle(self):
        nc._BOOTSTRAP_LAST_ATTEMPT.clear()
        yield
        nc._BOOTSTRAP_LAST_ATTEMPT.clear()

    async def test_throttle_suppresses_second_call(self, db_session, monkeypatch):
        """A recent attempt within the window short-circuits to False."""
        model = f"throttle-{uuid4().hex[:8]}"
        nc._BOOTSTRAP_LAST_ATTEMPT[(model, 512)] = datetime.now(UTC)
        # Should never reach the DB / enqueue.
        enqueue = AsyncMock(return_value=True)
        monkeypatch.setattr(nc, "enqueue_recalibration_dedup", enqueue)

        result = await maybe_trigger_bootstrap(db_session, model, 512)
        assert result is False
        enqueue.assert_not_called()

    async def test_stale_throttle_does_not_suppress(self, db_session, monkeypatch):
        """An attempt older than the window does not suppress (records a new one)."""
        model = f"stale-{uuid4().hex[:8]}"
        old = datetime.now(UTC) - timedelta(seconds=nc._BOOTSTRAP_COUNT_THROTTLE_SEC + 60)
        nc._BOOTSTRAP_LAST_ATTEMPT[(model, 512)] = old
        monkeypatch.setattr(nc, "enqueue_recalibration_dedup", AsyncMock(return_value=False))
        # No context -> returns False but it DID get past the throttle and
        # refreshed the timestamp.
        result = await maybe_trigger_bootstrap(db_session, model, 512)
        assert result is False
        assert nc._BOOTSTRAP_LAST_ATTEMPT[(model, 512)] > old

    async def test_no_context_returns_false(self, db_session, monkeypatch):
        model = f"nob-{uuid4().hex[:8]}"
        enqueue = AsyncMock(return_value=True)
        monkeypatch.setattr(nc, "enqueue_recalibration_dedup", enqueue)
        result = await maybe_trigger_bootstrap(db_session, model, 512)
        assert result is False
        enqueue.assert_not_called()

    async def test_below_gate_returns_false(self, db_session, monkeypatch):
        """Largest context < BOOTSTRAP_MIN_MEMORIES -> no enqueue."""
        model = f"belowgate-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session, n_memories=5, embedding_model=model, embedding_dimensions=512
        )
        enqueue = AsyncMock(return_value=True)
        monkeypatch.setattr(nc, "enqueue_recalibration_dedup", enqueue)

        result = await maybe_trigger_bootstrap(db_session, model, 512)
        assert result is False
        enqueue.assert_not_called()

    async def test_at_gate_enqueues(self, db_session, monkeypatch):
        """Largest context >= BOOTSTRAP_MIN_MEMORIES -> enqueue, return its result."""
        model = f"atgate-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session,
            n_memories=BOOTSTRAP_MIN_MEMORIES,
            embedding_model=model,
            embedding_dimensions=512,
        )
        enqueue = AsyncMock(return_value=True)
        monkeypatch.setattr(nc, "enqueue_recalibration_dedup", enqueue)

        result = await maybe_trigger_bootstrap(db_session, model, 512)
        assert result is True
        enqueue.assert_awaited_once_with(model, 512, context_id=None)

    async def test_enqueue_dedup_skip_propagates_false(self, db_session, monkeypatch):
        """When the gate fires but Redis dedup skips, the False is propagated."""
        model = f"dedupskip-{uuid4().hex[:8]}"
        await _make_context_with_memories(
            db_session,
            n_memories=BOOTSTRAP_MIN_MEMORIES,
            embedding_model=model,
            embedding_dimensions=512,
        )
        monkeypatch.setattr(nc, "enqueue_recalibration_dedup", AsyncMock(return_value=False))
        result = await maybe_trigger_bootstrap(db_session, model, 512)
        assert result is False
