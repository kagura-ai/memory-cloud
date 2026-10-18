"""An embedding run's claim on a memory row (#2041).

``process_pending_embedding`` claims a row by setting it ``processing`` and
stamping ``embedding_attempted_at``. The embedding sweep reclaims a
``processing`` row whose stamp is older than ``STALE_CLAIM_SECONDS``: that is the
crash recovery for a worker that died mid-run. The stamp is also the run's
claim token. The chunk writer's superseded checks and the run's outcome writes
match on it, so a reclaimed run neither writes its chunks over the newer
claim's nor records its status on it. (Its summary-point upsert is not fenced;
see ``content_chunk_writer``.)

A run that is only slow (a slow provider, a large chunk batch) must not be
reclaimed for it: it would be fenced out, the newer run would be reclaimed in
turn, and no run would ever record its outcome. So a live run renews its stamp
every ``RENEW_INTERVAL_SECONDS``, for at most ``MAX_RENEW_SECONDS`` — past that
the run is treated as hung and left to the sweep. A renewal is an UPDATE
fenced on the current stamp, in its own session (it never touches the run's
transaction), and it stops for good once the claim is gone (an edit or a
migration switch re-queued the row). A lock serialises each renewal with each
check of the stamp, so a check never compares a value a renewal is replacing.

A renewal waits at most ``RENEW_CHECKOUT_TIMEOUT_SECONDS`` for a pooled
connection (#2070): without that bound, a starved pool makes it wait the full
``DB_POOL_TIMEOUT``, and a single failed renewal could already let the stamp
go stale.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import update

from db.base import isolated_session
from models.memory import Memory
from utils.logger import get_logger

logger = get_logger(__name__)

# A ``processing`` row whose stamp is older than this is reclaimed: by the
# embedding sweep and by a new run's claim (crash recovery).
STALE_CLAIM_SECONDS = 60.0
# Renewals start on a fixed cadence, one interval after the previous one
# started however long it waited, and each waits at most
# RENEW_CHECKOUT_TIMEOUT_SECONDS for a connection. After a renewal that
# started at T (its stamp is no earlier), the next ones start at T+15, T+30
# and T+45: two can fail outright and the third has its connection by T+50,
# inside the cutoff (#2070).
RENEW_INTERVAL_SECONDS = 15.0
RENEW_CHECKOUT_TIMEOUT_SECONDS = 5.0
# Longer than any healthy run; a run still going after this is presumed hung.
MAX_RENEW_SECONDS = 600.0


class EmbeddingClaim:
    """This run's claim on one memory row: its stamp and the renewal of it.

    Read ``stamp`` only once the renewal is stopped (``stop_renewal``), or
    inside ``held()`` while it runs.
    """

    def __init__(self, db: Any, memory_id: UUID, stamp: datetime) -> None:
        self._db = db
        self.memory_id = memory_id
        self.stamp = stamp
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._renewing = False
        # When a renewal found the claim gone; it stops renewing for good.
        self._gone_seen_at: datetime | None = None

    @asynccontextmanager
    async def held(self) -> AsyncIterator[datetime]:
        """The current stamp, with renewals held off until the block ends."""
        async with self._lock:
            yield self.stamp

    def loss_reason(self) -> str:
        """Why the claim was lost, for ``embedding_claim_lost`` (#2070).

        Call once the renewal is stopped. A row is reclaimed (by the sweep or
        a new run's claim) only once its stamp is ``STALE_CLAIM_SECONDS`` old,
        so a loss seen while the stamp was younger is a re-queue — an edit, a
        migration switch — or a delete: ``"requeued"``, expected. Seen later,
        the run may have been reclaimed: ``"stale"`` — its renewals failed (a
        starved pool) or stopped (``MAX_RENEW_SECONDS``: a hung provider).
        A live renewal sees a re-queue within one interval, so only a run
        whose renewals were not landing reports ``"stale"``.
        """
        from utils.datetime import utcnow

        seen_at = self._gone_seen_at or utcnow()
        age = (seen_at - self.stamp).total_seconds()
        return "stale" if age >= STALE_CLAIM_SECONDS else "requeued"

    def start_renewal(self) -> None:
        """Start renewing the stamp in the background (once the claim is committed)."""
        if self._task is None and not self._stop.is_set():
            self._task = asyncio.create_task(self._renew_loop())

    async def stop_renewal(self) -> None:
        """Stop renewing, so ``stamp`` is final.

        A renewal already writing (it holds the lock) is waited for; one still
        waiting for a pooled connection is cancelled instead.
        """
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        if not self._renewing:
            task.cancel()
        await asyncio.wait({task})

    async def _renew_loop(self) -> None:
        deadline = time.monotonic() + MAX_RENEW_SECONDS
        next_at = time.monotonic() + RENEW_INTERVAL_SECONDS
        while self._gone_seen_at is None:
            delay = next_at - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), delay)
                    return
                except TimeoutError:
                    pass
            # Checked here, not only by the wait: when an attempt overran the
            # interval there is no wait, and ``wait_for(..., 0)`` would cancel
            # the event's waiter before it ran and miss a stop already set
            # (#2070). Nor can ``stop_renewal``'s cancel be relied on: asyncpg
            # shields a connect from cancellation.
            if self._stop.is_set():
                return
            if time.monotonic() >= deadline:
                logger.warning("embedding_claim_renewal_expired", memory_id=str(self.memory_id))
                return
            # The cadence counts from each attempt's start, so a renewal that
            # waited for a connection does not push the next one back.
            next_at = time.monotonic() + RENEW_INTERVAL_SECONDS
            try:
                await self._renew()
            except Exception as e:
                logger.warning(
                    "embedding_claim_renew_failed",
                    memory_id=str(self.memory_id),
                    error_class=type(e).__name__,
                )

    async def _renew(self) -> None:
        """Move the stamp while this run still holds it; mark the claim gone if not."""
        from utils.datetime import utcnow

        async with isolated_session(self._db) as session:
            # Check out the connection before taking the lock: under pool
            # pressure the wait must not hold off the chunk writer's checks.
            # A bounded wait (#2070): this renewal fails fast and the next
            # one still starts inside the stale cutoff.
            async with asyncio.timeout(RENEW_CHECKOUT_TIMEOUT_SECONDS):
                await session.connection()
            async with self._lock:
                if self._stop.is_set():
                    return
                self._renewing = True
                try:
                    new_stamp = utcnow()
                    result = await session.execute(
                        update(Memory)
                        .where(
                            Memory.id == self.memory_id,
                            Memory.embedding_status == "processing",
                            Memory.embedding_attempted_at == self.stamp,
                        )
                        .values(embedding_attempted_at=new_stamp)
                        .returning(Memory.id)
                    )
                    renewed = result.scalar_one_or_none() is not None
                    await session.commit()
                    if renewed:
                        self.stamp = new_stamp
                    else:
                        # Re-queued or reclaimed; the outcome write logs it.
                        self._gone_seen_at = new_stamp
                finally:
                    self._renewing = False
