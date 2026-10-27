"""Background task: sweep pending embeddings.

Issue #76: Crash recovery for memories whose embedding processing was
interrupted (server restart, error, or create_task failure).
"""

from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from utils.logger import get_logger

logger = get_logger(__name__)


async def _log_unsearchable_backlog(db) -> None:
    """Emit how many memories are saved but not searchable (#1496).

    A failed embedding never reaches Qdrant, and the BM25 sparse vectors live
    there too — so these rows are missing from recall in BOTH semantic and
    keyword mode. They are still counted by the dashboard and charged against
    quota, which is why the discrepancy is invisible from inside the product.

    Before this, the only way to discover the condition was to query the
    database by hand. It was found that way, with 467 rows already stuck.

    Best-effort: this is a diagnostic, and it must never be the reason the
    sweep does not run. Counts over the existing partial index.

    #2015 D6: the failures are also split by the error code that
    ``embedding_failure_values`` puts first: ``capped`` (the embedding spend
    cap, ``QUOTA-002``), ``configuration`` (``CFG-*`` / ``FEAT-*``: no
    credential or plan feature, and an unreachable self-hosted endpoint,
    which the retry policy treats the same way — ``is_configuration_failure``;
    all of these are retried without spending the budget) and ``other`` — so
    a capped workspace is not read as an unsearchable one. Rows failed before
    the prefix existed count as other.

    #2363: ``erased`` counts the surviving memories an account erasure took
    out of the queue (``ERASED_AUTHOR_EMBEDDING_ERROR``, exact match). They
    sit at the retry ceiling by design, so they are part of ``stalled`` too,
    but not of ``other``: they are not a provider problem. They alone do not
    raise the warning — they stay ``failed`` for good, and a line on every
    tick for something that needs no action would bury the signal; when it
    fires for other rows, it carries their count.
    """
    from sqlalchemy import func, or_

    from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR, MAX_EMBEDDING_RETRIES
    from models.memory import Memory

    erased = Memory.embedding_error == ERASED_AUTHOR_EMBEDDING_ERROR
    capped = Memory.embedding_error.like("QUOTA-002:%")
    configuration = or_(Memory.embedding_error.like("CFG-%"), Memory.embedding_error.like("FEAT-%"))
    try:
        row = (
            await db.execute(
                select(
                    func.count().label("unsearchable"),
                    func.count()
                    .filter(Memory.embedding_retry_count >= MAX_EMBEDDING_RETRIES)
                    .label("stalled"),
                    func.count().filter(capped).label("capped"),
                    func.count().filter(configuration).label("configuration"),
                    func.count().filter(erased).label("erased"),
                ).where(
                    Memory.embedding_status == "failed",
                    Memory.deleted_at.is_(None),
                )
            )
        ).one()
    except Exception as e:  # pragma: no cover - diagnostic must not break the sweep
        logger.debug("unsearchable_backlog_query_failed", error=str(e))
        return

    unsearchable, stalled = row.unsearchable, row.stalled
    if unsearchable - row.erased:
        logger.warning(
            "embedding_unsearchable_backlog",
            unsearchable=unsearchable,
            stalled=stalled,
            capped=row.capped,
            configuration=row.configuration,
            erased=row.erased,
            other=unsearchable - row.capped - row.configuration - row.erased,
        )


CLAIM_EXPIRED_ERROR = "claim_expired: the run outlived its claim on every retry"


async def _fail_exhausted_reclaims(db: AsyncSession, stale_cutoff: datetime, now: datetime) -> None:
    """Fail the stale ``processing`` rows that have spent their retry budget (#2095).

    A reclaim spends one retry, and the claim only reclaims a row with budget
    left, so a row whose runs keep outliving their claims ends up here. Each
    one is logged as ``embedding_budget_exhausted`` (the alertable event), and
    ``_log_unsearchable_backlog`` then counts it as stalled. The admin retry
    resets it. A failure is logged and rolled back so the sweep still runs.
    """
    from sqlalchemy import update

    from models.memory import Memory
    from services.memory_service import embedding_reclaim_exhausted_clause

    try:
        result = await db.execute(
            update(Memory)
            .where(Memory.deleted_at.is_(None), embedding_reclaim_exhausted_clause(stale_cutoff))
            .values(
                embedding_status="failed",
                embedding_error=CLAIM_EXPIRED_ERROR,
                # #1852: the pipeline's own clock; updated_at is an edit's.
                embedding_attempted_at=now,
            )
            .returning(Memory.id, Memory.embedding_retry_count)
            .execution_options(synchronize_session=False)
        )
        rows = result.all()
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error("embedding_reclaim_exhaust_failed", error=str(e), exc_info=True)
        return
    for memory_id, retry_count in rows:
        logger.error(
            "embedding_budget_exhausted",
            memory_id=str(memory_id),
            reason="claim_expired",
            retry_count=retry_count,
        )


async def sweep_pending_embeddings() -> None:
    """Find and process memories stuck in pending/processing/failed status.

    - pending older than 10s: create_task likely failed or never fired
    - processing older than STALE_CLAIM_SECONDS: worker crashed mid-processing (stale);
      a reclaim spends one retry (#2095), and a stale row with no budget left
      is failed here (``_fail_exhausted_reclaims``)
    - failed past the retry backoff with budget left (#979): a transient
      embedding/Qdrant blip self-heals instead of needing the manual admin
      retry endpoint, bounded by MAX_EMBEDDING_RETRIES so a poison row stops.
    process_pending_embedding() re-checks each gate atomically in its claim,
    so this SELECT is only a candidate prefilter (no double-increment race).
    """
    from sqlalchemy import and_, case, or_

    from db.base import get_db
    from models.memory import Memory
    from services.embedding_claim import STALE_CLAIM_SECONDS
    from services.memory_service import (
        embedding_reclaim_eligible_clause,
        embedding_retry_eligible_clause,
        process_pending_embedding,
    )
    from utils.datetime import utcnow

    async for db in get_db():
        try:
            now = utcnow()
            pending_cutoff = now - timedelta(seconds=10)
            stale_cutoff = now - timedelta(seconds=STALE_CLAIM_SECONDS)
            # #2095: a stale processing row with no budget left can no longer
            # be claimed, so no run would ever write its `failed`. Do it here,
            # on the claim's own stale cutoff, before the backlog count (so
            # this tick's count includes them) and the candidate query.
            await _fail_exhausted_reclaims(db, stale_cutoff, now)

            # #1496: report the backlog BEFORE the candidate query, because the
            # `if not pending_ids: return` below is exactly the state that
            # needs reporting — a deployment whose failed rows have all gone
            # terminal has nothing to sweep and would otherwise say nothing at
            # all while a growing share of its memories are unsearchable.
            #
            # `stalled` is the number nothing will ever retry on its own.
            # Emitted only when non-zero, so a healthy deployment stays quiet
            # and the line's presence is itself the signal.
            await _log_unsearchable_backlog(db)

            result = await db.execute(
                select(Memory.id)
                .where(
                    Memory.deleted_at.is_(None),
                    or_(
                        and_(
                            Memory.embedding_status == "pending",
                            Memory.created_at < pending_cutoff,
                        ),
                        # #979 / #2095: both shared with the claim gate so
                        # they cannot drift.
                        embedding_reclaim_eligible_clause(stale_cutoff),
                        embedding_retry_eligible_clause(now),
                    ),
                )
                # #979: process genuinely-pending/processing rows before failed
                # retries so a backlog of failed rows can't starve the limit(20)
                # budget and delay brand-new memories' first embedding.
                .order_by(
                    case((Memory.embedding_status == "failed", 1), else_=0),
                    Memory.created_at,
                )
                .limit(20)
            )
            pending_ids = [row[0] for row in result.all()]

            if not pending_ids:
                return

            logger.info("sweep_pending_embeddings", count=len(pending_ids))

            # Per-id try/except so one stuck embedding does not abort
            # the sweep — the next sweep tick (30s later) would re-pick
            # only this id, but in the meantime the OTHER pending ids
            # would sit untouched. Caught via Copilot review on #496.
            failed = 0
            for mid in pending_ids:
                try:
                    await process_pending_embedding(mid)
                except Exception as e:  # noqa: BLE001 — we want to keep going
                    failed += 1
                    logger.error(
                        "sweep_pending_embeddings_item_failed",
                        memory_id=str(mid),
                        error=str(e),
                        exc_info=True,
                    )

            logger.info(
                "sweep_pending_embeddings_done",
                processed=len(pending_ids) - failed,
                failed=failed,
            )

        except Exception as e:
            logger.error("sweep_pending_embeddings_failed", error=str(e), exc_info=True)


class _SeedState:
    """Whether this process has run the one-time gap seed (#2107).

    The seed runs on the leader's first reconcile tick and again on every
    tick until it completes once without an error (its own marker row keeps
    any later run, here or after a restart, from stamping again).
    """

    done = False


async def reconcile_content_chunk_gaps() -> None:
    """Re-queue the memories a context's inactive chunks left without them (#2107).

    First, until it has completed once in this process, the one-time seed of
    gaps that predate #2107 (``seed_content_chunk_gaps``: gated deployments
    only, marker-guarded). Then one tick of
    ``services.content_chunk_gaps.reconcile_content_chunk_gaps``: contexts
    with a gap stamp whose chunks are active again get a page of their long
    rows checked against their chunk points, and the rows without current
    chunks go back to the embedding sweep. Logged only when it did something;
    a failure is logged, rolled back, and the next tick retries.
    """
    from db.base import get_db
    from services.content_chunk_gaps import reconcile_content_chunk_gaps as reconcile
    from services.content_chunk_gaps import seed_content_chunk_gaps as seed

    async for db in get_db():
        if not _SeedState.done:
            try:
                await seed(db)
                _SeedState.done = True
            except Exception as e:
                await db.rollback()
                logger.error("content_chunks_gap_seed_failed", error=str(e), exc_info=True)
        try:
            result = await reconcile(db)
            if result.scanned or result.cleared or result.failed:
                logger.info(
                    "content_chunks_gap_reconcile_done",
                    stamped=result.stamped,
                    active=result.active,
                    scanned=result.scanned,
                    requeued=result.requeued,
                    cleared=result.cleared,
                    deferred=result.deferred,
                    failed=result.failed,
                )
        except Exception as e:
            await db.rollback()
            logger.error("content_chunks_gap_reconcile_tick_failed", error=str(e), exc_info=True)


def schedule_embedding_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the embedding sweep and the content chunk gap reconciler (which seeds once)."""
    scheduler.add_job(
        sweep_pending_embeddings,
        trigger=IntervalTrigger(seconds=30),
        id="sweep_pending_embeddings",
        name="Sweep Pending Embeddings",
        replace_existing=True,
    )
    logger.info("scheduled_embedding_sweep_task", interval_seconds=30)
    scheduler.add_job(
        reconcile_content_chunk_gaps,
        trigger=IntervalTrigger(seconds=60),
        id="reconcile_content_chunk_gaps",
        name="Reconcile Content Chunk Gaps",
        replace_existing=True,
    )
    logger.info("scheduled_content_chunks_gap_task", interval_seconds=60)
