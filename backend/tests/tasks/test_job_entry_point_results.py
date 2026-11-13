"""What each scheduled job entry point returns to the run recorder (Issue #2768).

A job that handles its own failure (logs, rolls back, returns normally) now
returns ``job_failed(exc)`` so the run is recorded as ``failed`` with the
exception class; on success it returns the counts it already logged; a
single-flight skip returns ``{"skipped": 1}``. Nothing else about the jobs
changes: the existing swallow tests in the per-family files still pass.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tasks.job_runs import JobFailed, job_failed
from tests.tasks.conftest import mock_get_db_factory


def _failed(cls: type[BaseException]) -> JobFailed:
    return job_failed(cls())


def _db(*, rowcount: int = 0, execute_error: BaseException | None = None) -> MagicMock:
    db = MagicMock()
    if execute_error is not None:
        db.execute = AsyncMock(side_effect=execute_error)
    else:
        db.execute = AsyncMock(return_value=MagicMock(rowcount=rowcount))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _single_flight(acquired: bool):
    @asynccontextmanager
    async def guard(_key: str):
        yield acquired

    return guard


def _failing_single_flight():
    @asynccontextmanager
    async def guard(_key: str):
        raise ConnectionError("lock unavailable")
        yield  # pragma: no cover

    return guard


async def _no_pages(_repo):
    return
    yield  # pragma: no cover


async def _failing_pages(_repo):
    raise RuntimeError("graph page read failed")
    yield  # pragma: no cover


# ---------------------------------------------------------------------------
# Cleanup jobs (device codes, email tokens, known devices, public MCP usage)
# ---------------------------------------------------------------------------


class TestCleanupJobs:
    async def test_device_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import device_code_tasks

        monkeypatch.setattr(device_code_tasks, "get_db", mock_get_db_factory(_db(rowcount=7)))
        assert await device_code_tasks.cleanup_expired_device_codes_task() == {"deleted": 7}
        monkeypatch.setattr(
            device_code_tasks, "get_db", mock_get_db_factory(_db(execute_error=OSError()))
        )
        assert await device_code_tasks.cleanup_expired_device_codes_task() == _failed(OSError)

    async def test_email_action_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import email_action_token_tasks as mod

        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db(rowcount=2)))
        assert await mod.cleanup_email_action_tokens_task() == {
            "deleted": 2,
            "signup_requests_deleted": 2,
        }
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db(execute_error=OSError())))
        assert await mod.cleanup_email_action_tokens_task() == _failed(OSError)

    async def test_known_devices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import known_device_tasks as mod

        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db(rowcount=3)))
        assert await mod.cleanup_stale_known_devices_task() == {"deleted": 3}
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db(execute_error=OSError())))
        assert await mod.cleanup_stale_known_devices_task() == _failed(OSError)

    async def test_public_mcp_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import public_mcp_usage_tasks as mod

        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db()))
        monkeypatch.setattr(mod, "purge_public_mcp_usage_events", AsyncMock(return_value=5))
        assert await mod.purge_public_mcp_usage_events_task() == {"deleted": 5}
        monkeypatch.setattr(
            mod, "purge_public_mcp_usage_events", AsyncMock(side_effect=TimeoutError())
        )
        assert await mod.purge_public_mcp_usage_events_task() == _failed(TimeoutError)


# ---------------------------------------------------------------------------
# Operation history, referral, semantic edges, erasure, BM25 drift
# ---------------------------------------------------------------------------


class TestOperationHistory:
    async def test_counts_skip_and_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import operation_history_tasks as mod

        counts = {"deleted": 4, "batches": 1, "budget_exhausted": 0}
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db()))
        monkeypatch.setattr(mod, "single_flight", _single_flight(True))
        monkeypatch.setattr(mod, "purge_operation_history", AsyncMock(return_value=counts))
        assert await mod.purge_operation_history_task() == counts

        monkeypatch.setattr(mod, "purge_operation_history", AsyncMock(side_effect=OSError()))
        assert await mod.purge_operation_history_task() == _failed(OSError)

        monkeypatch.setattr(mod, "single_flight", _single_flight(False))
        assert await mod.purge_operation_history_task() == {"skipped": 1}


class TestReferral:
    async def test_counts_and_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import referral_tasks as mod

        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db()))
        result = mod.PendingGrantSweep(candidates=2, users_applied=1, grants_applied=3, busy=1)
        monkeypatch.setattr(mod, "sweep_pending_referral_grants", AsyncMock(return_value=result))
        assert await mod.apply_pending_referral_grants_task() == {
            "candidates": 2,
            "users_applied": 1,
            "grants_applied": 3,
            "busy": 1,
            "failed": 0,
        }
        monkeypatch.setattr(
            mod, "sweep_pending_referral_grants", AsyncMock(side_effect=ConnectionError())
        )
        assert await mod.apply_pending_referral_grants_task() == _failed(ConnectionError)


class TestSemanticEdgeReverify:
    async def test_counts_and_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import semantic_edge_reverify as mod

        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db()))
        monkeypatch.setattr(
            mod,
            "semantic_edge_reverify_run",
            AsyncMock(return_value={"semantic_edges_deleted": 6}),
        )
        assert await mod._scheduled_entrypoint() == {"semantic_edges_deleted": 6}

        monkeypatch.setattr(mod, "semantic_edge_reverify_run", AsyncMock(side_effect=OSError()))
        assert await mod._scheduled_entrypoint() == _failed(OSError)

        def broken_get_db():
            raise ConnectionError("no session")

        monkeypatch.setattr(mod, "get_db", broken_get_db)
        assert await mod._scheduled_entrypoint() == _failed(ConnectionError)


class TestErasureSweep:
    async def test_counts_and_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks.erasure_tasks import sweep_pending_erasures

        service = MagicMock()
        service.sweep_pending_erasures = AsyncMock(return_value=2)
        with (
            patch("db.base.get_db", mock_get_db_factory(_db())),
            patch("services.account_erasure_service.AccountErasureService", return_value=service),
            patch("services.vector_erasure.drain_vector_erasures", AsyncMock()),
        ):
            assert await sweep_pending_erasures() == {"executed": 2}
            service.sweep_pending_erasures = AsyncMock(side_effect=OSError())
            assert await sweep_pending_erasures() == _failed(OSError)


class TestBm25Drift:
    async def test_counts_and_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from config.settings import Settings
        from tasks import bm25_drift_tasks as mod

        monkeypatch.setattr(
            mod, "get_settings", lambda: Settings(_env_file=None, bm25_drift_cron_enabled=True)
        )
        db = _db()
        db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[("c1",)])))
        orchestrator = MagicMock()
        orchestrator.run = AsyncMock()
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(db))
        with patch(
            "services.bm25_drift.orchestrator.Bm25DriftOrchestrator", return_value=orchestrator
        ):
            assert await mod.bm25_drift_maintenance_task() == {
                "contexts": 1,
                "runs": 1,
                "errors": 0,
            }
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db(execute_error=OSError())))
        assert await mod.bm25_drift_maintenance_task() == _failed(OSError)


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------


class TestSleepMaintenance:
    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("SLEEP_ENABLED", "true")

    async def test_task_returns_the_run_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import sleep_tasks as mod

        counts = {"contexts": 3, "runs": 2, "errors": 1}
        monkeypatch.setattr(mod, "single_flight", _single_flight(True))
        monkeypatch.setattr(mod, "_sleep_maintenance_run", AsyncMock(return_value=counts))
        assert await mod.sleep_maintenance_task() == counts

    async def test_task_skip_and_lock_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import sleep_tasks as mod

        monkeypatch.setattr(mod, "single_flight", _single_flight(False))
        assert await mod.sleep_maintenance_task() == {"skipped": 1}
        monkeypatch.setattr(mod, "single_flight", _failing_single_flight())
        assert await mod.sleep_maintenance_task() == _failed(ConnectionError)

    async def test_run_counts_and_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import sleep_tasks as mod

        db = _db()
        db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(db))
        config = SimpleNamespace(tag_cooccurrence_enabled=False)
        with (
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=config)),
            patch("neural.config.NeuralMemoryConfig.invalidate_cache"),
        ):
            assert await mod._sleep_maintenance_run() == {"contexts": 0, "runs": 0, "errors": 0}
            monkeypatch.setattr(
                mod, "get_db", mock_get_db_factory(_db(execute_error=TimeoutError()))
            )
            assert await mod._sleep_maintenance_run() == _failed(TimeoutError)


# ---------------------------------------------------------------------------
# Embedding sweep and chunk-gap reconcile, resource indexer
# ---------------------------------------------------------------------------


class TestEmbeddingJobs:
    async def test_sweep_returns_zeros_when_nothing_is_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tasks import embedding_tasks

        monkeypatch.setattr(embedding_tasks, "_fail_exhausted_reclaims", AsyncMock())
        db = _db()
        result = MagicMock()
        result.all.return_value = []
        result.one.return_value = SimpleNamespace(unsearchable=0, stalled=0, erased=0)
        db.execute = AsyncMock(return_value=result)
        with patch("db.base.get_db", mock_get_db_factory(db)):
            assert await embedding_tasks.sweep_pending_embeddings() == {
                "processed": 0,
                "failed": 0,
            }

    async def test_sweep_counts_and_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from uuid import uuid4

        from tasks import embedding_tasks

        monkeypatch.setattr(embedding_tasks, "_fail_exhausted_reclaims", AsyncMock())
        monkeypatch.setattr(embedding_tasks, "_log_unsearchable_backlog", AsyncMock())
        db = _db()
        ids = [uuid4(), uuid4(), uuid4()]
        db.execute = AsyncMock(
            return_value=MagicMock(all=MagicMock(return_value=[(i,) for i in ids]))
        )
        process = AsyncMock(side_effect=[None, RuntimeError("item"), None])
        with (
            patch("db.base.get_db", mock_get_db_factory(db)),
            patch("services.memory_service.process_pending_embedding", process),
        ):
            assert await embedding_tasks.sweep_pending_embeddings() == {
                "processed": 2,
                "failed": 1,
            }
        with patch("db.base.get_db", mock_get_db_factory(_db(execute_error=OSError()))):
            assert await embedding_tasks.sweep_pending_embeddings() == _failed(OSError)

    async def test_reconcile_counts_and_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.content_chunk_gaps import GapReconcileResult
        from tasks import embedding_tasks

        monkeypatch.setattr(embedding_tasks._SeedState, "done", True)
        tick = AsyncMock(return_value=GapReconcileResult(scanned=3, requeued=1))
        with (
            patch("db.base.get_db", mock_get_db_factory(_db())),
            patch("services.content_chunk_gaps.reconcile_content_chunk_gaps", tick),
        ):
            assert await embedding_tasks.reconcile_content_chunk_gaps() == {
                "stamped": 0,
                "active": 0,
                "scanned": 3,
                "requeued": 1,
                "cleared": 0,
                "deferred": 0,
                "failed": 0,
            }
        with (
            patch("db.base.get_db", mock_get_db_factory(_db())),
            patch(
                "services.content_chunk_gaps.reconcile_content_chunk_gaps",
                AsyncMock(side_effect=OSError()),
            ),
        ):
            assert await embedding_tasks.reconcile_content_chunk_gaps() == _failed(OSError)

    async def test_a_failed_seed_does_not_fail_the_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.content_chunk_gaps import GapReconcileResult
        from tasks import embedding_tasks

        monkeypatch.setattr(embedding_tasks._SeedState, "done", False)
        with (
            patch("db.base.get_db", mock_get_db_factory(_db())),
            patch(
                "services.content_chunk_gaps.seed_content_chunk_gaps",
                AsyncMock(side_effect=OSError()),
            ),
            patch(
                "services.content_chunk_gaps.reconcile_content_chunk_gaps",
                AsyncMock(return_value=GapReconcileResult()),
            ),
        ):
            result = await embedding_tasks.reconcile_content_chunk_gaps()
        assert isinstance(result, dict) and result["failed"] == 0


class TestResourceIndexer:
    async def test_nothing_queued_and_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import resource_indexer_job as mod

        db = _db()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(db))
        assert await mod.run_queued_indexers() == {"picked": 0, "failed": 0, "rate_limited": 0}
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db(execute_error=OSError())))
        assert await mod.run_queued_indexers() == _failed(OSError)


# ---------------------------------------------------------------------------
# Neural jobs
# ---------------------------------------------------------------------------


class TestNeuralJobs:
    @pytest.fixture(autouse=True)
    def _neural_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("ENABLE_DECAY", "true")

    async def test_weight_decay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import neural_tasks as mod

        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db()))
        monkeypatch.setattr(mod.NeuralMemoryConfig, "from_db", AsyncMock(return_value=MagicMock()))
        monkeypatch.setattr(mod, "_graph_pages", _no_pages)
        assert await mod.weight_decay_task() == {"graphs": 0, "updated_graphs": 0}
        monkeypatch.setattr(mod, "_graph_pages", _failing_pages)
        assert await mod.weight_decay_task() == _failed(RuntimeError)

    async def test_consolidation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import neural_tasks as mod

        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db()))
        monkeypatch.setattr(mod, "_graph_pages", _no_pages)
        with patch("services.memory_service.MemoryService", return_value=MagicMock()):
            assert await mod.consolidation_task() == {"users": 0, "promoted": 0, "deleted": 0}
            monkeypatch.setattr(mod, "_graph_pages", _failing_pages)
            assert await mod.consolidation_task() == _failed(RuntimeError)

    async def test_cleanup_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tasks import neural_tasks as mod

        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db(rowcount=9)))
        assert await mod.cleanup_deleted_memories_task() == {"purged": 9}
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db(execute_error=OSError())))
        assert await mod.cleanup_deleted_memories_task() == _failed(OSError)

    async def test_orphan_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.orphan_vector_sweep import CollectionSweep, SweepResult
        from tasks import neural_tasks as mod

        monkeypatch.delenv("ORPHAN_VECTOR_SWEEP_ENABLED", raising=False)
        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db()))
        result = SweepResult(
            dry_run=False,
            grace=timedelta(hours=1),
            collections=[CollectionSweep(collection="c", scanned=10, no_row=2, deleted=2)],
        )
        assert (result.scanned, result.orphans, result.deleted) == (10, 2, 2)
        with (
            patch("tasks.single_flight.single_flight", _single_flight(True)),
            patch(
                "services.orphan_vector_sweep.sweep_orphan_points",
                AsyncMock(return_value=result),
            ),
        ):
            counts = await mod.sweep_orphan_vectors_task()
        assert isinstance(counts, dict)
        assert (counts["scanned"], counts["orphans"], counts["deleted"]) == (10, 2, 2)
        with patch("tasks.single_flight.single_flight", _single_flight(False)):
            assert await mod.sweep_orphan_vectors_task() == {"skipped": 1}
        with (
            patch("tasks.single_flight.single_flight", _single_flight(True)),
            patch(
                "services.orphan_vector_sweep.sweep_orphan_points",
                AsyncMock(side_effect=OSError()),
            ),
        ):
            assert await mod.sweep_orphan_vectors_task() == _failed(OSError)

    async def test_deferred_point_drain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.deferred_point_deletes import DrainResult
        from tasks import neural_tasks as mod

        monkeypatch.setattr(mod, "get_db", mock_get_db_factory(_db()))
        drained = DrainResult(drained=2, kept=1, pending=3, oldest_age_seconds=40.0)
        with patch(
            "services.deferred_point_deletes.drain_deferred_point_deletes",
            AsyncMock(return_value=drained),
        ):
            assert await mod.drain_deferred_point_deletes_task() == {
                "drained": 2,
                "kept": 1,
                "pending": 3,
                "busy": 0,
                "failed": 0,
            }
        # A delete the vector store refused (or that timed out) is caught by
        # the drain, which returns normally: the run stays "ok" (job-level
        # outcome) but its counts must show the failure, as the other sweeps'
        # counts do.
        refused = DrainResult(kept=0, pending=4, failed="OSError")
        with patch(
            "services.deferred_point_deletes.drain_deferred_point_deletes",
            AsyncMock(return_value=refused),
        ):
            assert await mod.drain_deferred_point_deletes_task() == {
                "drained": 0,
                "kept": 0,
                "pending": 4,
                "busy": 0,
                "failed": 1,
            }
        busy = DrainResult(pending=2, busy=True)
        with patch(
            "services.deferred_point_deletes.drain_deferred_point_deletes",
            AsyncMock(return_value=busy),
        ):
            assert await mod.drain_deferred_point_deletes_task() == {
                "drained": 0,
                "kept": 0,
                "pending": 2,
                "busy": 1,
                "failed": 0,
            }
        with patch(
            "services.deferred_point_deletes.drain_deferred_point_deletes",
            AsyncMock(side_effect=OSError()),
        ):
            assert await mod.drain_deferred_point_deletes_task() == _failed(OSError)
