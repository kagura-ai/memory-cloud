"""Branch-coverage tests for ``tasks.sleep_tasks``.

The existing ``test_sleep_tasks.py`` pins the cross-process single-flight guard
(Issue #933) and the ``single_flight`` helper semantics. This file targets the
*uncovered* branches of the module:

- ``_refresh_hub_tag_cache``: real-Postgres exercise of the hub-tag SQL +
  upsert (empty scope, untagged-only scope, a genuine hub tag, the duplicate-
  tag dedupe path, and the on-conflict upsert path).
- ``sleep_maintenance_task``: the ENABLE_NEURAL_MEMORY-disabled and
  SLEEP_ENABLED-disabled early-return guards.
- ``_sleep_maintenance_run``: the per-context orchestrator loop, including the
  per-context error/rollback branch, the workspace/context skip branch, the
  hub-tag-refresh loop (refresh success + per-context failure + dedupe), and
  the outer ``get_db`` failure handler.
- ``schedule_sleep_tasks``: the disabled (no-op) branch and the enabled branch
  with default and custom cron env vars.

All external work (orchestrator, NeuralMemoryConfig, single_flight, the
scheduler) is mocked. DB rows for the hub-tag test are created through the real
``db_session`` fixture (workspace + context + memories) so the SQL itself is
verified, not a stub.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# Importing ``tasks.sleep_tasks`` pulls in the whole ``tasks`` package
# __init__, which imports numpy (via ``neural``) and mcp.types (a pydantic
# RootModel). Under ``--cov`` instrumentation that import chain is fragile:
# numpy's C extension can hit "cannot load module more than once" and
# pydantic's ``create_generic_submodel`` can KeyError on ``pydantic.root_model``
# if those modules are first touched mid-instrumentation. Pre-importing them
# here (numpy fully, then pydantic.root_model) makes them already-present in
# sys.modules before the heavy chain runs, so coverage collection is stable.
import numpy  # noqa: F401  isort: skip
import pydantic.root_model  # noqa: F401  isort: skip

from tasks.sleep_tasks import (
    _refresh_hub_tag_cache,
    _sleep_maintenance_run,
    schedule_sleep_tasks,
    sleep_maintenance_task,
)


@asynccontextmanager
async def _fake_single_flight(acquired: bool):
    yield acquired


@pytest.fixture(autouse=True)
async def _ensure_hub_tag_table(async_engine):
    """Create the ``hub_tag_cache`` table for this module.

    The session ``create_all`` in tests/conftest.py runs before this module
    imports ``models.hub_tag``, so the table may not be registered when the
    schema is built. Create it explicitly (idempotent) so the real-DB
    ``_refresh_hub_tag_cache`` upsert has a target table.
    """
    from models.hub_tag import HubTagCache

    async with async_engine.begin() as conn:
        await conn.run_sync(HubTagCache.__table__.create, checkfirst=True)


async def _seed_ws_ctx(db):
    """Create a workspace + context and return (workspace_id, context_id) as UUIDs."""
    from models.auth import Context, Workspace

    ws = Workspace(id=uuid4(), name=f"ws-{uuid4().hex[:8]}", owner_user_id="hub-owner")
    db.add(ws)
    await db.flush()
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by="hub-owner",
    )
    db.add(ctx)
    await db.flush()
    return ws.id, ctx.id


def _make_memory(*, workspace_id, context_id, tags, user_id="hub-owner", deleted=False):
    from models.memory import Memory
    from utils.datetime import utcnow

    return Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        context_id=context_id,
        summary="s",
        content="c",
        type="note",
        tags=tags,
        scope="working",
        client="test",
        deleted_at=utcnow() if deleted else None,
    )


class TestRefreshHubTagCacheRealDB:
    """``_refresh_hub_tag_cache`` against a real Postgres session."""

    async def test_returns_zero_and_writes_empty_when_no_tagged_memories(self, db_session):
        """A context with only untagged memories: memory_count counted, hub_tags []."""
        from sqlalchemy import select

        from models.hub_tag import HubTagCache

        ws_id, ctx_id = await _seed_ws_ctx(db_session)
        # Three untagged memories — denominator is 3, no tags above threshold.
        for _ in range(3):
            db_session.add(_make_memory(workspace_id=ws_id, context_id=ctx_id, tags=None))
        await db_session.flush()

        n = await _refresh_hub_tag_cache(
            db_session, workspace_id=str(ws_id), context_id=str(ctx_id), threshold=0.5
        )
        await db_session.flush()

        assert n == 0
        row = (
            await db_session.execute(select(HubTagCache).where(HubTagCache.context_id == ctx_id))
        ).scalar_one()
        assert row.hub_tags == []
        assert row.memory_count == 3
        assert row.threshold_used == 0.5

    async def test_identifies_hub_tag_above_threshold(self, db_session):
        """A tag on >50% of memories is a hub tag; a rare tag is not."""
        ws_id, ctx_id = await _seed_ws_ctx(db_session)
        # 'common' on 3/4 memories (0.75 > 0.5 → hub); 'rare' on 1/4 (not hub).
        db_session.add(_make_memory(workspace_id=ws_id, context_id=ctx_id, tags=["common"]))
        db_session.add(_make_memory(workspace_id=ws_id, context_id=ctx_id, tags=["common"]))
        db_session.add(_make_memory(workspace_id=ws_id, context_id=ctx_id, tags=["common", "rare"]))
        db_session.add(_make_memory(workspace_id=ws_id, context_id=ctx_id, tags=None))
        await db_session.flush()

        n = await _refresh_hub_tag_cache(
            db_session, workspace_id=str(ws_id), context_id=str(ctx_id), threshold=0.5
        )
        await db_session.flush()

        assert n == 1
        from sqlalchemy import select

        from models.hub_tag import HubTagCache

        row = (
            await db_session.execute(select(HubTagCache).where(HubTagCache.context_id == ctx_id))
        ).scalar_one()
        assert row.hub_tags == ["common"]
        assert row.memory_count == 4

    async def test_duplicate_tags_in_one_memory_counted_once(self, db_session):
        """A memory whose tags array repeats a value must not over-count it.

        With ['python', 'python'] on the single tagged memory of two total, the
        DISTINCT-id dedupe means 'python' appears in 1/2 memories = 0.5, which is
        NOT > 0.5 → not a hub tag. If raw unnest were counted it would be 2/2.
        """
        ws_id, ctx_id = await _seed_ws_ctx(db_session)
        db_session.add(
            _make_memory(workspace_id=ws_id, context_id=ctx_id, tags=["python", "python"])
        )
        db_session.add(_make_memory(workspace_id=ws_id, context_id=ctx_id, tags=None))
        await db_session.flush()

        n = await _refresh_hub_tag_cache(
            db_session, workspace_id=str(ws_id), context_id=str(ctx_id), threshold=0.5
        )
        assert n == 0  # 0.5 is not strictly greater than 0.5

    async def test_deleted_memories_excluded_from_denominator_and_counts(self, db_session):
        """Soft-deleted memories are out of scope and out of the denominator."""
        ws_id, ctx_id = await _seed_ws_ctx(db_session)
        # 1 live tagged memory, 5 deleted ones. Denominator = 1 → tag is 1/1 = hub.
        db_session.add(_make_memory(workspace_id=ws_id, context_id=ctx_id, tags=["solo"]))
        for _ in range(5):
            db_session.add(
                _make_memory(workspace_id=ws_id, context_id=ctx_id, tags=["solo"], deleted=True)
            )
        await db_session.flush()

        n = await _refresh_hub_tag_cache(
            db_session, workspace_id=str(ws_id), context_id=str(ctx_id), threshold=0.5
        )
        assert n == 1
        from sqlalchemy import select

        from models.hub_tag import HubTagCache

        row = (
            await db_session.execute(select(HubTagCache).where(HubTagCache.context_id == ctx_id))
        ).scalar_one()
        assert row.memory_count == 1
        assert row.hub_tags == ["solo"]

    async def test_upsert_overwrites_existing_row(self, db_session):
        """A second refresh on the same (ws, ctx) updates the existing row in place."""
        from sqlalchemy import func, select

        from models.hub_tag import HubTagCache

        ws_id, ctx_id = await _seed_ws_ctx(db_session)
        db_session.add(_make_memory(workspace_id=ws_id, context_id=ctx_id, tags=["x"]))
        await db_session.flush()

        await _refresh_hub_tag_cache(
            db_session, workspace_id=str(ws_id), context_id=str(ctx_id), threshold=0.5
        )
        await db_session.flush()

        # Add a second memory NOT carrying 'x' so 'x' drops to 1/2 = not hub.
        db_session.add(_make_memory(workspace_id=ws_id, context_id=ctx_id, tags=None))
        await db_session.flush()

        await _refresh_hub_tag_cache(
            db_session, workspace_id=str(ws_id), context_id=str(ctx_id), threshold=0.5
        )
        await db_session.flush()

        # Still exactly one row (upsert, not insert) and it reflects the new state.
        count = (
            await db_session.execute(
                select(func.count())
                .select_from(HubTagCache)
                .where(HubTagCache.context_id == ctx_id)
            )
        ).scalar_one()
        assert count == 1
        row = (
            await db_session.execute(select(HubTagCache).where(HubTagCache.context_id == ctx_id))
        ).scalar_one()
        assert row.memory_count == 2
        assert row.hub_tags == []

    async def test_none_row_branch_writes_empty(self, db_session):
        """If the SQL returns no row (defensive None branch), write empty hub set.

        We patch ``db.execute`` so the SELECT's ``.one_or_none()`` yields None,
        then let the real INSERT run. This exercises lines 140-142.
        """
        ws_id, ctx_id = await _seed_ws_ctx(db_session)

        real_execute = db_session.execute
        call = {"n": 0}

        async def fake_execute(stmt, *args, **kwargs):
            call["n"] += 1
            if call["n"] == 1:
                # First execute is the hub-tag SELECT → force one_or_none() == None.
                res = MagicMock()
                res.one_or_none = MagicMock(return_value=None)
                return res
            return await real_execute(stmt, *args, **kwargs)

        with patch.object(db_session, "execute", side_effect=fake_execute):
            n = await _refresh_hub_tag_cache(
                db_session, workspace_id=str(ws_id), context_id=str(ctx_id), threshold=0.5
            )
        await db_session.flush()

        assert n == 0
        from sqlalchemy import select

        from models.hub_tag import HubTagCache

        row = (
            await db_session.execute(select(HubTagCache).where(HubTagCache.context_id == ctx_id))
        ).scalar_one()
        assert row.hub_tags == []
        assert row.memory_count == 0


class TestSleepMaintenanceTaskGuards:
    """Env-flag early-return guards in ``sleep_maintenance_task``."""

    async def test_skips_when_neural_memory_disabled(self, monkeypatch):
        """ENABLE_NEURAL_MEMORY != true → return before touching the lock/DB."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "false")
        monkeypatch.setenv("SLEEP_ENABLED", "true")

        from tasks import sleep_tasks

        sf = MagicMock()
        get_db = MagicMock()
        with (
            patch.object(sleep_tasks, "single_flight", sf),
            patch.object(sleep_tasks, "get_db", get_db),
        ):
            await sleep_maintenance_task()

        sf.assert_not_called()
        get_db.assert_not_called()

    async def test_skips_when_sleep_disabled(self, monkeypatch):
        """SLEEP_ENABLED != true → return even though neural memory is on."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("SLEEP_ENABLED", "false")

        from tasks import sleep_tasks

        sf = MagicMock()
        get_db = MagicMock()
        with (
            patch.object(sleep_tasks, "single_flight", sf),
            patch.object(sleep_tasks, "get_db", get_db),
        ):
            await sleep_maintenance_task()

        sf.assert_not_called()
        get_db.assert_not_called()

    async def test_neural_memory_default_unset_is_treated_as_disabled(self, monkeypatch):
        """Unset ENABLE_NEURAL_MEMORY defaults to 'false' → skip."""
        monkeypatch.delenv("ENABLE_NEURAL_MEMORY", raising=False)
        monkeypatch.setenv("SLEEP_ENABLED", "true")

        from tasks import sleep_tasks

        get_db = MagicMock()
        with patch.object(sleep_tasks, "get_db", get_db):
            await sleep_maintenance_task()

        get_db.assert_not_called()


class TestSleepMaintenanceRun:
    """The extracted ``_sleep_maintenance_run`` sweep body."""

    def _make_db(self, contexts):
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        result = MagicMock()
        result.all = MagicMock(return_value=contexts)
        db.execute = AsyncMock(return_value=result)
        return db

    def _patch_get_db(self, sleep_tasks, db):
        async def _get_db():
            yield db

        return patch.object(sleep_tasks, "get_db", _get_db)

    async def test_runs_orchestrator_per_context_and_commits(self):
        """Two valid contexts → orchestrator.run + commit fire once each."""
        from tasks import sleep_tasks

        ws1, ctx1 = uuid4(), uuid4()
        ws2, ctx2 = uuid4(), uuid4()
        contexts = [("u1", ws1, ctx1), ("u2", ws2, ctx2)]
        db = self._make_db(contexts)

        cfg = MagicMock()
        cfg.tag_cooccurrence_enabled = False

        orch_instance = MagicMock()
        orch_instance.run = AsyncMock()
        orch_cls = MagicMock(return_value=orch_instance)

        with (
            self._patch_get_db(sleep_tasks, db),
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=cfg)),
            patch("neural.config.NeuralMemoryConfig.invalidate_cache", MagicMock()),
            patch("services.sleep.orchestrator.SleepOrchestrator", orch_cls),
        ):
            await _sleep_maintenance_run()

        assert orch_instance.run.await_count == 2
        assert db.commit.await_count == 2
        db.rollback.assert_not_awaited()

    async def test_skips_context_with_null_workspace_or_context(self):
        """Rows with NULL workspace_id or context_id are skipped, not run."""
        from tasks import sleep_tasks

        good_ws, good_ctx = uuid4(), uuid4()
        contexts = [
            ("u1", None, good_ctx),  # null workspace → skip
            ("u2", good_ws, None),  # null context → skip
            ("u3", good_ws, good_ctx),  # valid → run
        ]
        db = self._make_db(contexts)

        cfg = MagicMock()
        cfg.tag_cooccurrence_enabled = False

        orch_instance = MagicMock()
        orch_instance.run = AsyncMock()
        orch_cls = MagicMock(return_value=orch_instance)

        with (
            self._patch_get_db(sleep_tasks, db),
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=cfg)),
            patch("neural.config.NeuralMemoryConfig.invalidate_cache", MagicMock()),
            patch("services.sleep.orchestrator.SleepOrchestrator", orch_cls),
        ):
            await _sleep_maintenance_run()

        # Only the single fully-populated row triggered a run.
        assert orch_instance.run.await_count == 1
        assert db.commit.await_count == 1

    async def test_orchestrator_failure_rolls_back_and_continues(self):
        """One context's orchestrator raises → rollback + continue to the next."""
        from tasks import sleep_tasks

        ws1, ctx1 = uuid4(), uuid4()
        ws2, ctx2 = uuid4(), uuid4()
        contexts = [("u1", ws1, ctx1), ("u2", ws2, ctx2)]
        db = self._make_db(contexts)

        cfg = MagicMock()
        cfg.tag_cooccurrence_enabled = False

        orch_instance = MagicMock()
        orch_instance.run = AsyncMock(side_effect=[RuntimeError("boom"), None])
        orch_cls = MagicMock(return_value=orch_instance)

        with (
            self._patch_get_db(sleep_tasks, db),
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=cfg)),
            patch("neural.config.NeuralMemoryConfig.invalidate_cache", MagicMock()),
            patch("services.sleep.orchestrator.SleepOrchestrator", orch_cls),
        ):
            await _sleep_maintenance_run()

        # First failed (rollback), second succeeded (commit). No exception escaped.
        assert orch_instance.run.await_count == 2
        db.rollback.assert_awaited_once()
        db.commit.assert_awaited_once()

    async def test_hub_tag_refresh_runs_when_enabled_and_dedupes_ws_ctx(self):
        """tag_cooccurrence_enabled → hub refresh once per (ws, ctx), deduped."""
        from tasks import sleep_tasks

        ws, ctx = uuid4(), uuid4()
        other_ctx = uuid4()
        # Same (ws, ctx) appears twice (two users) → refresh once for it;
        # a second distinct context → refresh again. Total 2 refreshes.
        contexts = [
            ("u1", ws, ctx),
            ("u2", ws, ctx),
            ("u3", ws, other_ctx),
        ]
        db = self._make_db(contexts)

        cfg = MagicMock()
        cfg.tag_cooccurrence_enabled = True
        cfg.tag_cooccurrence_hub_threshold = 0.5

        orch_instance = MagicMock()
        orch_instance.run = AsyncMock()
        orch_cls = MagicMock(return_value=orch_instance)

        refresh = AsyncMock(return_value=3)

        with (
            self._patch_get_db(sleep_tasks, db),
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=cfg)),
            patch("neural.config.NeuralMemoryConfig.invalidate_cache", MagicMock()),
            patch("services.sleep.orchestrator.SleepOrchestrator", orch_cls),
            patch.object(sleep_tasks, "_refresh_hub_tag_cache", refresh),
        ):
            await _sleep_maintenance_run()

        # Deduped: 2 distinct (ws, ctx) keys → 2 refresh calls.
        assert refresh.await_count == 2
        called_keys = {(kw["workspace_id"], kw["context_id"]) for _, kw in refresh.await_args_list}
        assert called_keys == {(str(ws), str(ctx)), (str(ws), str(other_ctx))}
        # Orchestrator still runs for all 3 user rows.
        assert orch_instance.run.await_count == 3

    async def test_hub_tag_refresh_failure_rolls_back_and_continues(self):
        """A hub-tag refresh that raises → rollback + warning, run continues."""
        from tasks import sleep_tasks

        ws, ctx1 = uuid4(), uuid4()
        ctx2 = uuid4()
        contexts = [("u1", ws, ctx1), ("u2", ws, ctx2)]
        db = self._make_db(contexts)

        cfg = MagicMock()
        cfg.tag_cooccurrence_enabled = True
        cfg.tag_cooccurrence_hub_threshold = 0.5

        orch_instance = MagicMock()
        orch_instance.run = AsyncMock()
        orch_cls = MagicMock(return_value=orch_instance)

        # First refresh raises, second succeeds.
        refresh = AsyncMock(side_effect=[RuntimeError("hub boom"), 1])

        with (
            self._patch_get_db(sleep_tasks, db),
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=cfg)),
            patch("neural.config.NeuralMemoryConfig.invalidate_cache", MagicMock()),
            patch("services.sleep.orchestrator.SleepOrchestrator", orch_cls),
            patch.object(sleep_tasks, "_refresh_hub_tag_cache", refresh),
        ):
            await _sleep_maintenance_run()

        assert refresh.await_count == 2
        # At least one rollback happened for the failed hub refresh.
        assert db.rollback.await_count >= 1
        # Both orchestrator runs still fired.
        assert orch_instance.run.await_count == 2

    async def test_hub_tag_refresh_skips_rows_with_null_scope(self):
        """Inside the hub-refresh loop, NULL ws/ctx rows are skipped (continue)."""
        from tasks import sleep_tasks

        good_ws, good_ctx = uuid4(), uuid4()
        contexts = [
            ("u1", None, good_ctx),  # null ws → skip in hub loop
            ("u2", good_ws, None),  # null ctx → skip in hub loop
            ("u3", good_ws, good_ctx),  # valid → refresh
        ]
        db = self._make_db(contexts)

        cfg = MagicMock()
        cfg.tag_cooccurrence_enabled = True
        cfg.tag_cooccurrence_hub_threshold = 0.5

        orch_instance = MagicMock()
        orch_instance.run = AsyncMock()
        orch_cls = MagicMock(return_value=orch_instance)

        refresh = AsyncMock(return_value=0)

        with (
            self._patch_get_db(sleep_tasks, db),
            patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=cfg)),
            patch("neural.config.NeuralMemoryConfig.invalidate_cache", MagicMock()),
            patch("services.sleep.orchestrator.SleepOrchestrator", orch_cls),
            patch.object(sleep_tasks, "_refresh_hub_tag_cache", refresh),
        ):
            await _sleep_maintenance_run()

        # Only the one valid (ws, ctx) row triggered a hub refresh.
        assert refresh.await_count == 1

    async def test_outer_get_db_failure_is_swallowed(self):
        """An exception from the get_db iteration is caught and logged, not raised."""
        from tasks import sleep_tasks

        async def _boom_get_db():
            raise ConnectionError("db unreachable")
            yield  # pragma: no cover

        with patch.object(sleep_tasks, "get_db", _boom_get_db):
            # Must NOT raise — the outer try/except logs and returns.
            await _sleep_maintenance_run()


class TestSleepMaintenanceTaskRunsBody:
    """End-to-end: the lock-acquired path drives the sweep body."""

    async def test_lock_acquired_runs_sweep_body(self, monkeypatch):
        """Lock acquired → _sleep_maintenance_run executes (get_db iterated)."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("SLEEP_ENABLED", "true")

        from tasks import sleep_tasks

        ran = {"called": False}

        async def _fake_run():
            ran["called"] = True

        with (
            patch.object(sleep_tasks, "single_flight", lambda key: _fake_single_flight(True)),
            patch.object(sleep_tasks, "_sleep_maintenance_run", _fake_run),
        ):
            await sleep_maintenance_task()

        assert ran["called"] is True

    async def test_lock_acquisition_failure_is_swallowed_and_logged(self, monkeypatch):
        """single_flight raising (e.g. Postgres down) → caught, logged, NOT raised.

        Covers the outer ``except Exception`` in ``sleep_maintenance_task`` that
        wraps the lock acquisition — distinct from the inner sweep handler. The
        sweep body must never run when the lock context itself fails.
        """
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("SLEEP_ENABLED", "true")

        from tasks import sleep_tasks

        @asynccontextmanager
        async def _raising_single_flight(key):
            raise ConnectionError("postgres down")
            yield True  # pragma: no cover

        run = AsyncMock()
        with (
            patch.object(sleep_tasks, "single_flight", _raising_single_flight),
            patch.object(sleep_tasks, "_sleep_maintenance_run", run),
        ):
            # Must NOT propagate — APScheduler keeps running.
            await sleep_maintenance_task()

        run.assert_not_awaited()

    async def test_lock_not_acquired_skips_sweep_body(self, monkeypatch):
        """Lock held elsewhere → _sleep_maintenance_run is NOT invoked."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("SLEEP_ENABLED", "true")

        from tasks import sleep_tasks

        run = AsyncMock()
        with (
            patch.object(sleep_tasks, "single_flight", lambda key: _fake_single_flight(False)),
            patch.object(sleep_tasks, "_sleep_maintenance_run", run),
        ):
            await sleep_maintenance_task()

        run.assert_not_awaited()


class TestScheduleSleepTasks:
    """``schedule_sleep_tasks`` registration branches."""

    def test_not_scheduled_when_sleep_disabled(self, monkeypatch):
        """SLEEP_ENABLED != true → no job added."""
        monkeypatch.setenv("SLEEP_ENABLED", "false")
        scheduler = MagicMock()
        schedule_sleep_tasks(scheduler)
        scheduler.add_job.assert_not_called()

    def test_schedules_with_default_cron(self, monkeypatch):
        """Enabled with no cron overrides → job at hour=2, minute=0."""
        monkeypatch.setenv("SLEEP_ENABLED", "true")
        monkeypatch.delenv("SLEEP_CRON_HOUR", raising=False)
        monkeypatch.delenv("SLEEP_CRON_MINUTE", raising=False)
        scheduler = MagicMock()

        schedule_sleep_tasks(scheduler)

        scheduler.add_job.assert_called_once()
        kwargs = scheduler.add_job.call_args.kwargs
        assert kwargs["id"] == "sleep_maintenance"
        assert kwargs["replace_existing"] is True
        trigger = kwargs["trigger"]
        fields = {f.name: str(f) for f in trigger.fields}
        assert fields["hour"] == "2"
        assert fields["minute"] == "0"

    def test_schedules_with_custom_cron(self, monkeypatch):
        """Custom SLEEP_CRON_HOUR/MINUTE are parsed into the CronTrigger."""
        monkeypatch.setenv("SLEEP_ENABLED", "true")
        monkeypatch.setenv("SLEEP_CRON_HOUR", "5")
        monkeypatch.setenv("SLEEP_CRON_MINUTE", "30")
        scheduler = MagicMock()

        schedule_sleep_tasks(scheduler)

        trigger = scheduler.add_job.call_args.kwargs["trigger"]
        fields = {f.name: str(f) for f in trigger.fields}
        assert fields["hour"] == "5"
        assert fields["minute"] == "30"
