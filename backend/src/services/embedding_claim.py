"""An embedding run's claim on a memory row (#2041).

``process_pending_embedding`` claims a row by setting it ``processing`` and
stamping ``embedding_attempted_at``. The embedding sweep reclaims a
``processing`` row whose stamp is older than the 60 s stale cutoff: that is the
crash recovery for a worker that died mid-run. The stamp is also the run's
claim token. The chunk writer's superseded checks and the run's outcome writes
match on it, so the result of a reclaimed run never lands on the newer claim.

A run that is only slow (a slow provider, a large chunk batch) must not be
reclaimed for it: it would be fenced out, the newer run would be reclaimed in
turn, and no run would ever record its outcome. So a live run renews its stamp
every ``RENEW_INTERVAL_SECONDS``, for at most ``MAX_RENEW_SECONDS`` — past that
the run is treated as hung and left to the sweep. A renewal is an UPDATE
fenced on the current stamp, in its own session (it never touches the run's
transaction), and it stops for good once the claim is gone (an edit or a
migration switch re-queued the row). A lock serialises each renewal with each
check of the stamp, so a check never compares a value a renewal is replacing.
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

# Well inside the 60 s stale cutoff: two renewals can fail before a reclaim.
RENEW_INTERVAL_SECONDS = 20.0
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

    @asynccontextmanager
    async def held(self) -> AsyncIterator[datetime]:
        """The current stamp, with renewals held off until the block ends."""
        async with self._lock:
            yield self.stamp

    def start_renewal(self) -> None:
        """Start renewing the stamp in the background (once the claim is committed)."""
        if self._task is None and not self._stop.is_set():
            self._task = asyncio.create_task(self._renew_loop())

    async def stop_renewal(self) -> None:
        """Stop renewing; waits for a renewal in flight, so ``stamp`` is final."""
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _renew_loop(self) -> None:
        deadline = time.monotonic() + MAX_RENEW_SECONDS
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), RENEW_INTERVAL_SECONDS)
                return
            except TimeoutError:
                pass
            if time.monotonic() >= deadline:
                logger.warning("embedding_claim_renewal_expired", memory_id=str(self.memory_id))
                return
            async with self._lock:
                if self._stop.is_set():
                    return
                try:
                    renewed = await self._renew()
                except Exception as e:
                    logger.warning(
                        "embedding_claim_renew_failed",
                        memory_id=str(self.memory_id),
                        error_class=type(e).__name__,
                    )
                    continue
                if renewed is None:
                    return  # The claim is gone; the outcome write logs it.
                self.stamp = renewed

    async def _renew(self) -> datetime | None:
        """Move the stamp while this run still holds it; None when it does not."""
        from utils.datetime import utcnow

        new_stamp = utcnow()
        async with isolated_session(self._db) as session:
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
        return new_stamp if renewed else None
