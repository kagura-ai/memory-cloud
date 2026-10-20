"""Background task: sweep pending embeddings.

Issue #76: Crash recovery for memories whose embedding processing was
interrupted (server restart, error, or create_task failure).
"""

from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

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
    """
    from sqlalchemy import func, or_

    from config.constants import MAX_EMBEDDING_RETRIES
    from models.memory import Memory

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
    if unsearchable:
        logger.warning(
            "embedding_unsearchable_backlog",
            unsearchable=unsearchable,
            stalled=stalled,
            capped=row.capped,
            configuration=row.configuration,
            other=unsearchable - row.capped - row.configuration,
        )


async def sweep_pending_embeddings() -> None:
    """Find and process memories stuck in pending/processing/failed status.

    - pending older than 10s: create_task likely failed or never fired
    - processing older than STALE_CLAIM_SECONDS: worker crashed mid-processing (stale)
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
        embedding_clock_before,
        embedding_retry_eligible_clause,
        process_pending_embedding,
    )
    from utils.datetime import utcnow

    async for db in get_db():
        try:
            now = utcnow()

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

            pending_cutoff = now - timedelta(seconds=10)
            stale_cutoff = now - timedelta(seconds=STALE_CLAIM_SECONDS)
            result = await db.execute(
                select(Memory.id)
                .where(
                    Memory.deleted_at.is_(None),
                    or_(
                        and_(
                            Memory.embedding_status == "pending",
                            Memory.created_at < pending_cutoff,
                        ),
                        and_(
                            Memory.embedding_status == "processing",
                            embedding_clock_before(stale_cutoff),
                        ),
                        # #979: shared with the claim gate so they cannot drift.
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


async def reconcile_content_chunk_gaps() -> None:
    """Re-queue the memories a context's inactive chunks left without them (#2107).

    One tick of ``services.content_chunk_gaps.reconcile_content_chunk_gaps``:
    contexts with a gap stamp whose chunks are active again get a page of
    their long rows checked against their chunk points, and the rows without
    current chunks go back to the embedding sweep. Logged only when it did
    something; a failure is logged and the next tick retries.
    """
    from db.base import get_db
    from services.content_chunk_gaps import reconcile_content_chunk_gaps as reconcile

    async for db in get_db():
        try:
            result = await reconcile(db)
            if result.scanned or result.cleared or result.failed:
                logger.info(
                    "content_chunk_gap_reconcile_done",
                    stamped=result.stamped,
                    active=result.active,
                    scanned=result.scanned,
                    requeued=result.requeued,
                    cleared=result.cleared,
                    deferred=result.deferred,
                    failed=result.failed,
                )
        except Exception as e:
            logger.error("content_chunk_gap_reconcile_failed", error=str(e), exc_info=True)


async def seed_content_chunk_gaps() -> None:
    """Stamp the contexts whose content chunk gaps predate #2107, once (gated only).

    ``services.content_chunk_gaps.seed_content_chunk_gaps``: a one-shot job the
    scheduler leader runs when it starts; the marker it writes keeps later
    starts from stamping again. A failure is logged; the next start retries.
    """
    from db.base import get_db
    from services.content_chunk_gaps import seed_content_chunk_gaps as seed

    async for db in get_db():
        try:
            await seed(db)
        except Exception as e:
            logger.error("content_chunk_gap_seed_failed", error=str(e), exc_info=True)


def schedule_embedding_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the embedding sweep, the content chunk gap reconciler and its one-time seed."""
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
    logger.info("scheduled_content_chunk_gap_task", interval_seconds=60)
    # #2107: one-shot at start (the leader's), before the first reconcile tick.
    scheduler.add_job(
        seed_content_chunk_gaps,
        trigger=DateTrigger(),
        id="seed_content_chunk_gaps",
        name="Seed Content Chunk Gaps",
        replace_existing=True,
    )
