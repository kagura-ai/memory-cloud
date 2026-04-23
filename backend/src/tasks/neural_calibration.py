"""Background calibration task for kNN-seed percentile threshold (#406 Phase B).

Runs on three triggers (issue #240 C2 / #406 step 5):

- **Bootstrap**: the first time a context with ``(model, dimensions)``
  crosses the D3 gate (≥ 200 effective memories OR ≥ 10k observations),
  ``_create_knn_seed_edges`` enqueues a one-shot calibration here.
- **Admin manual**: ``POST /api/admin/neural/recalibrate`` enqueues from
  the HTTP handler.
- **Lazy TTL**: ``resolve_knn_threshold`` enqueues when it serves a
  ``valid_until``-expired row.

All three paths funnel through :func:`enqueue_recalibration_dedup` so
concurrent requests produce at most one running task per ``(model_name,
dimensions, context_id)`` combination. The dedup key is a Redis
``SETNX`` with a 1-hour TTL — the compute itself typically completes in
seconds (200 Qdrant top-k searches, each returning up to 50 neighbors;
total observations ≤ 10k), and the 1-hour window just absorbs retry
storms while a task is in-flight.

The actual compute reuses the helpers from
``scripts/measure_embedding_threshold.py``. For model-global
calibration (``context_id=None``) the job picks the **largest context**
that uses this ``(model, dimensions)`` pair and samples from it, on the
assumption (D1) that the similarity distribution is broadly context-
independent for a given embedding model. A per-context calibration
(``context_id != None``) would sample from exactly that context — the
schema supports it, the runtime lookup in :mod:`neural.calibration`
does not (D5 v2 follow-up).
"""

from __future__ import annotations

import asyncio
import importlib.util
import secrets
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from types import ModuleType
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from db.qdrant import get_qdrant_client
from db.redis import get_redis_client
from models.config import ContextSearchConfig
from models.memory import Memory
from models.neural import EmbeddingCalibration
from neural.config import NeuralMemoryConfig
from utils.logger import get_logger

logger = get_logger(__name__)

# Dedup lock TTL. A calibration compute is short (seconds), but the lock
# lives long enough to absorb retry storms while the compute is in-flight.
_DEDUP_LOCK_TTL_SEC = 3600


@cache
def _load_measure_script() -> ModuleType:
    """Load ``scripts/measure_embedding_threshold.py`` as a module.

    Uses ``importlib.util.spec_from_file_location`` with an explicit path
    rather than a ``sys.path`` insert. The script itself also calls
    ``sys.path.insert(0, "src")`` at import time (it's written as a
    standalone CLI), so we snapshot/restore ``sys.path`` around
    ``exec_module`` to ensure the long-running API worker's import path
    ends exactly as it started.

    ``functools.cache`` memoizes the no-argument call so subsequent
    ``compute_calibration`` invocations reuse the already-loaded module
    without re-executing its top-level imports (numpy, sqlalchemy,
    qdrant-client etc.).
    """
    import sys  # noqa: PLC0415 — local to the snapshot/restore window

    backend_root = Path(__file__).resolve().parent.parent.parent
    script_file = backend_root / "scripts" / "measure_embedding_threshold.py"
    spec = importlib.util.spec_from_file_location(
        "_kagura_measure_embedding_threshold", script_file
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"unable to build import spec for {script_file} — calibration cannot run"
        )
    module = importlib.util.module_from_spec(spec)
    original_sys_path = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        # Restore even if the script mutated sys.path during import. Copy
        # the list so later mutations by anyone else don't affect the
        # snapshot we saved.
        sys.path[:] = original_sys_path
    return module


# Strong references to in-flight calibration tasks. ``asyncio.create_task``
# returns a task that the event loop only holds a weak reference to, so
# without keeping a strong ref the task can be garbage-collected mid-flight
# and the compute silently vanishes (see Python asyncio docs). We drop the
# reference via ``add_done_callback`` once the task finishes.
_IN_FLIGHT_TASKS: set[asyncio.Task] = set()

# D3 gate thresholds — also defined in the Phase A script but duplicated
# here so the task module doesn't import from the ``scripts/`` tree at
# runtime (only ``scripts/measure_embedding_threshold.py`` imports are
# deferred and treated as pure helpers).
BOOTSTRAP_MIN_MEMORIES = 200
BOOTSTRAP_MIN_OBSERVATIONS = 10_000

# Sampling parameters for the in-process calibration run. Match Phase A's
# acceptance defaults (``--memories 200 --top-k 50``) so the production
# values we ship with are statistically consistent with what the CLI
# script produced.
SAMPLE_MEMORIES = 200
SAMPLE_TOP_K = 50

# In-process bootstrap-count throttle. ``maybe_trigger_bootstrap`` fires on
# every ``remember()`` that hits D4 step 3 (pre-calibration phase), and each
# call runs a full-table ``COUNT(*)``. The Redis dedup lock prevents
# duplicate compute jobs but not duplicate counts. Cache the last attempt
# timestamp per (model, dimensions) and skip the count when seen recently.
# 5 minutes is long enough to absorb a burst of ingestion at a new
# context's cold start, short enough that a near-D3 context crosses the
# threshold within one dedup lock lifetime (1h).
_BOOTSTRAP_COUNT_THROTTLE_SEC = 300
_BOOTSTRAP_LAST_ATTEMPT: dict[tuple[str, int], datetime] = {}


def _dedup_key(model_name: str, dimensions: int, context_id: UUID | None) -> str:
    """Redis dedup key for a calibration job.

    Unique per ``(model_name, dimensions, context_id)`` — a per-context
    job does not preempt a pending model-global job and vice versa.
    """
    ctx_part = str(context_id) if context_id is not None else "global"
    return f"neural:calibrate:{model_name}:{dimensions}:{ctx_part}"


async def enqueue_recalibration_dedup(
    model_name: str,
    dimensions: int,
    context_id: UUID | None,
) -> bool:
    """Enqueue a calibration task with Redis-backed dedup.

    Returns ``True`` when this call acquired the lock and spawned the
    task; ``False`` when another caller is already running (or about to
    run) a job for the same key — the duplicate is silently dropped.
    Always returns quickly; the compute runs in an ``asyncio.create_task``.

    Args:
        model_name: Embedding model name (e.g. ``text-embedding-3-small``).
        dimensions: Vector dimensionality.
        context_id: Reserved for D5 v2 per-context calibration. Currently
            the call sites always pass ``None`` (model-global).
    """
    key = _dedup_key(model_name, dimensions, context_id)
    # Write a unique token (not a dummy "1") so release can check we still
    # own the lock via compare-and-delete. Without this, the finally branch
    # could blindly DELETE a key that another worker re-acquired after our
    # TTL expired mid-compute, defeating dedup under load.
    # (Copilot review PR #420 loop 5.)
    token = secrets.token_hex(16)
    try:
        client = get_redis_client()
        acquired = await client.set(key, token, nx=True, ex=_DEDUP_LOCK_TTL_SEC)
    except Exception as exc:
        # Redis unavailable → fail-open (run anyway). A duplicate compute
        # is wasteful but not incorrect, whereas skipping compute on a
        # Redis hiccup would stall the calibration path indefinitely.
        # Signal "we never owned a lock" by clearing the token so the
        # release branch skips the DEL entirely — it had nothing to
        # release and could only corrupt another worker's lock.
        logger.warning(
            "calibration_dedup_redis_error",
            key=key,
            error=str(exc),
        )
        acquired = True
        token = ""

    if not acquired:
        logger.debug("calibration_dedup_skipped", key=key)
        return False

    task = asyncio.create_task(
        _run_calibration(model_name, dimensions, context_id, dedup_key=key, token=token)
    )
    _IN_FLIGHT_TASKS.add(task)
    task.add_done_callback(_IN_FLIGHT_TASKS.discard)
    return True


async def _run_calibration(
    model_name: str,
    dimensions: int,
    context_id: UUID | None,
    dedup_key: str,
    token: str,
) -> None:
    """Execute the calibration compute + upsert, then release the dedup lock.

    Wraps :func:`compute_calibration` so the lock is always released even
    on exception paths. Any logging/observability happens inside
    ``compute_calibration``; this wrapper only handles the try/finally.

    ``token`` is the value written when :func:`enqueue_recalibration_dedup`
    acquired the lock. Release uses a compare-and-delete Lua script so we
    only remove keys we still own (TTL expiration + re-acquisition by
    another worker is the hazard this guards against). An empty token
    signals the fail-open path — we never acquired a lock, so we must not
    DELETE. (Copilot review PR #420 loop 5.)
    """
    try:
        async for db in get_db():
            await compute_calibration(db, model_name, dimensions, context_id)
            break
    except IntegrityError as exc:
        # Partial-unique-index collision. The Redis fail-open path and a
        # brief Redis outage can allow two workers to run ``compute_
        # calibration`` concurrently — both DELETE + INSERT race, and one
        # loses on the partial unique index. The other worker's row is
        # already correct, so log at info level and move on rather than
        # as a scary "calibration_task_failed" error. (Copilot review
        # PR #420 loop 4.)
        logger.info(
            "calibration_concurrent_upsert_race",
            model=model_name,
            dimensions=dimensions,
            context_id=str(context_id) if context_id else None,
            error=str(exc),
            hint="another worker won the upsert; the row is already correct",
        )
    except Exception as exc:
        logger.error(
            "calibration_task_failed",
            model=model_name,
            dimensions=dimensions,
            context_id=str(context_id) if context_id else None,
            error=str(exc),
            exc_info=True,
        )
    finally:
        await _release_dedup_lock(dedup_key, token)


# Compare-and-delete so a worker whose lock TTL expired (and was re-acquired
# by another worker) does NOT blow away that other worker's lock. Standard
# Redis SETNX lock release idiom. Returns 1 when we deleted our key, 0 when
# the key was missing, held by someone else, or already released.
_DEDUP_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)


async def _release_dedup_lock(key: str, token: str) -> None:
    """Compare-and-delete the Redis dedup lock.

    Skips the release entirely when ``token`` is empty (the fail-open
    path never acquired a lock and DEL would corrupt another worker's
    lock if one exists). Any Redis error during release is logged and
    swallowed — the TTL guarantees eventual release.
    """
    if not token:
        return
    try:
        client = get_redis_client()
        release_script = client.register_script(_DEDUP_RELEASE_SCRIPT)
        await release_script(keys=[key], args=[token])
    except Exception as exc:
        # Leaving the lock to TTL out is acceptable — next recalibration
        # fires in at most _DEDUP_LOCK_TTL_SEC.
        logger.warning(
            "calibration_dedup_release_failed",
            key=key,
            error=str(exc),
        )


async def compute_calibration(
    db: AsyncSession,
    model_name: str,
    dimensions: int,
    context_id: UUID | None,
) -> EmbeddingCalibration | None:
    """Measure percentiles + upsert the calibration row.

    Returns the upserted ``EmbeddingCalibration`` instance on success, or
    ``None`` if the D3 gate fails (insufficient sample) — the fallback
    chain keeps step 3 (disabled) in that case until the context grows.

    The model-global case picks the largest context using this ``(model,
    dimensions)`` pair as the sampling source. This is a v1 simplification
    of the "sample from all contexts" ideal — D1 says the distribution is
    broadly context-independent, so sampling from one large context is an
    acceptable proxy.
    """
    # Defer the script import so test environments don't need numpy etc.
    # if they never exercise this path. The script lives under ``scripts/``
    # which is not on the default ``sys.path``. Load it via importlib with an
    # explicit file-path spec so we don't mutate global ``sys.path`` — a
    # long-running API worker would otherwise accumulate a path entry that
    # can cause hard-to-debug module shadowing and makes imports depend on
    # call order (Copilot review PR #420 loop 2). After the first call the
    # module is cached in ``sys.modules`` under its own name, so subsequent
    # calls reuse the cached module for free.
    _script_module = _load_measure_script()
    compute_percentiles = _script_module.compute_percentiles
    fetch_vectors = _script_module.fetch_vectors
    measure_top_k = _script_module.measure_top_k
    sample_memories = _script_module.sample_memories

    sample_context_id = context_id
    if sample_context_id is None:
        sample_context_id = await _pick_largest_context_for_model(db, model_name, dimensions)
    if sample_context_id is None:
        logger.warning(
            "calibration_no_context_for_model",
            model=model_name,
            dimensions=dimensions,
            hint="No context has any memories with this (model, dimensions) yet.",
        )
        return None

    # Resolve the correct collection for Qdrant lookups.
    from db.qdrant import get_collection_name  # noqa: PLC0415

    collection = get_collection_name(model_name, dimensions)

    sampled = await sample_memories(db, sample_context_id, SAMPLE_MEMORIES)
    if not sampled:
        logger.warning(
            "calibration_no_memories_sampled",
            model=model_name,
            dimensions=dimensions,
            context_id=str(sample_context_id),
        )
        return None

    qdrant = get_qdrant_client()
    vectors = await fetch_vectors(qdrant, collection, [m.id for m in sampled])
    scores, effective = await measure_top_k(sampled, vectors, collection, SAMPLE_TOP_K)

    observations = len(scores)
    if effective < BOOTSTRAP_MIN_MEMORIES and observations < BOOTSTRAP_MIN_OBSERVATIONS:
        # D3 gate fails — distribution estimate too noisy. Don't write a
        # row, leave the runtime fallback chain at step 3 (disabled).
        logger.warning(
            "calibration_d3_gate_failed",
            model=model_name,
            dimensions=dimensions,
            effective_memories=effective,
            observations=observations,
            min_memories=BOOTSTRAP_MIN_MEMORIES,
            min_observations=BOOTSTRAP_MIN_OBSERVATIONS,
        )
        return None

    percentiles = compute_percentiles(scores)
    if not percentiles:
        logger.warning(
            "calibration_no_scores",
            model=model_name,
            dimensions=dimensions,
            context_id=str(sample_context_id),
        )
        return None

    config = await NeuralMemoryConfig.from_db(db)
    now = datetime.now(UTC)
    valid_until = now + timedelta(days=config.calibration_ttl_days)

    # Upsert: delete any existing row for this key, then insert fresh. Two
    # partial unique indexes (one for NULL context_id, one for non-NULL)
    # enforce at-most-one row per key from the DB side; the explicit
    # delete-then-insert is simpler than an ON CONFLICT dance because the
    # partial indexes don't participate in INSERT ... ON CONFLICT.
    del_stmt = delete(EmbeddingCalibration).where(
        EmbeddingCalibration.model_name == model_name,
        EmbeddingCalibration.dimensions == dimensions,
    )
    if context_id is None:
        del_stmt = del_stmt.where(EmbeddingCalibration.context_id.is_(None))
    else:
        del_stmt = del_stmt.where(EmbeddingCalibration.context_id == context_id)
    await db.execute(del_stmt)

    row = EmbeddingCalibration(
        model_name=model_name,
        dimensions=dimensions,
        context_id=context_id,
        p25=percentiles["p25"],
        p50=percentiles["p50"],
        p75=percentiles["p75"],
        p90=percentiles["p90"],
        p95=percentiles["p95"],
        p99=percentiles["p99"],
        sample_size=observations,
        sampled_at=now,
        valid_until=valid_until,
    )
    db.add(row)
    await db.commit()

    logger.info(
        "calibration_upserted",
        model=model_name,
        dimensions=dimensions,
        context_id=str(context_id) if context_id else None,
        p90=percentiles["p90"],
        sample_size=observations,
        valid_until=valid_until.isoformat(),
    )
    return row


async def _pick_largest_context_for_model(
    db: AsyncSession,
    model_name: str,
    dimensions: int,
) -> UUID | None:
    """Find the context with the most memories for a ``(model, dimensions)``.

    Model-global calibration samples from one representative context
    because D1 claims the distribution is broadly context-independent
    for a given embedding model. We pick the largest existing context so
    the sample has the best chance of clearing the D3 gate.

    Returns the chosen ``context_id``, or ``None`` if no context uses
    this ``(model, dimensions)`` pair yet (no row in
    ``ContextSearchConfig`` or the default-routed legacy path's context
    has zero successful memories).
    """
    stmt = (
        select(Memory.context_id, func.count(Memory.id).label("n"))
        .join(
            ContextSearchConfig,
            ContextSearchConfig.context_id == Memory.context_id,
            isouter=True,
        )
        .where(
            Memory.deleted_at.is_(None),
            Memory.embedding_status == "success",
            Memory.context_id.is_not(None),
            _model_dims_where(model_name, dimensions),
        )
        .group_by(Memory.context_id)
        .order_by(func.count(Memory.id).desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    return row[0]


def _model_dims_where(model_name: str, dimensions: int):
    """Build the WHERE predicate matching a context's ``(model, dimensions)``.

    The legacy default ``text-embedding-3-small / 512`` path has no
    ``ContextSearchConfig`` row — contexts that predate per-context
    routing simply fall through to the application default. Match them
    by treating a NULL config as the legacy default.
    """
    from sqlalchemy import and_, or_  # noqa: PLC0415

    is_legacy_default = model_name == "text-embedding-3-small" and dimensions == 512
    if is_legacy_default:
        return or_(
            ContextSearchConfig.context_id.is_(None),
            and_(
                ContextSearchConfig.embedding_model == model_name,
                ContextSearchConfig.embedding_dimensions == dimensions,
            ),
        )
    return and_(
        ContextSearchConfig.embedding_model == model_name,
        ContextSearchConfig.embedding_dimensions == dimensions,
    )


async def maybe_trigger_bootstrap(
    db: AsyncSession,
    model_name: str,
    dimensions: int,
) -> bool:
    """Enqueue a bootstrap calibration if the D3 gate is newly crossed.

    Called from ``_create_knn_seed_edges`` when
    ``resolve_knn_threshold`` returns ``None`` (step 3 of D4 — no
    calibration row yet). Counts memories across contexts that use this
    ``(model, dimensions)``; if the count is at or above
    :data:`BOOTSTRAP_MIN_MEMORIES`, kicks off a deduped job.

    This is the only path that runs per remember() call, so the cost of
    the count is kept small via the dedup lock: at most one bootstrap
    attempt per ``_DEDUP_LOCK_TTL_SEC`` (1 hour), even under heavy
    concurrent ingestion.

    Returns ``True`` if a job was enqueued by this call, ``False`` if
    the gate did not fire, the in-process throttle suppressed the count,
    or a Redis dedup skip occurred.
    """
    # In-process throttle on the COUNT query itself. Responsibility split:
    #
    #   - **this throttle** (in-process dict) collapses redundant COUNT
    #     queries from the same worker process into one DB round-trip
    #     per 5 minutes.
    #   - **Redis SETNX dedup** (inside ``enqueue_recalibration_dedup``
    #     one layer deeper) collapses redundant calibration compute jobs
    #     across workers and restarts into one running task per hour.
    #
    # Both layers are needed: the in-process throttle doesn't help a
    # second API worker (separate dict), and the Redis dedup lives below
    # this call site so it can't avoid the COUNT. 5 min window is short
    # enough that a near-D3 context still bootstraps within a single
    # Redis dedup lifetime (1h). (Copilot review PR #420 loop 3-4.)
    throttle_key = (model_name, dimensions)
    now = datetime.now(UTC)
    last = _BOOTSTRAP_LAST_ATTEMPT.get(throttle_key)
    if last is not None and (now - last).total_seconds() < _BOOTSTRAP_COUNT_THROTTLE_SEC:
        return False
    _BOOTSTRAP_LAST_ATTEMPT[throttle_key] = now

    count_stmt = (
        select(func.count(Memory.id))
        .join(
            ContextSearchConfig,
            ContextSearchConfig.context_id == Memory.context_id,
            isouter=True,
        )
        .where(
            Memory.deleted_at.is_(None),
            Memory.embedding_status == "success",
            # Exclude NULL-context memories so the count matches the set
            # ``_pick_largest_context_for_model`` samples from. Without this
            # filter a large NULL-context backfill (legacy pre-context
            # migrations or admin-inserted system rows) could prematurely
            # trip BOOTSTRAP_MIN_MEMORIES without any real context crossing
            # the D3 gate — the calibration job would then abort at
            # ``_pick_largest_context_for_model`` with no_context_for_model.
            Memory.context_id.is_not(None),
            _model_dims_where(model_name, dimensions),
        )
    )
    count = int((await db.execute(count_stmt)).scalar_one() or 0)
    if count < BOOTSTRAP_MIN_MEMORIES:
        return False

    return await enqueue_recalibration_dedup(model_name, dimensions, context_id=None)
