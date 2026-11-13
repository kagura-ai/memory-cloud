"""Admin operations status (Issue #2768).

``GET /api/v1/admin/operations/status`` — the state of operations in
aggregates only: every scheduled job's last run and outcome, every work
queue's size and oldest item, and every platform LLM lane's recorded usage and
latest opt-in probe. Behind the system-admin gate in every
``SYSTEM_ADMIN_MODE`` (``AdminUser``: an operator principal in ``service``
mode is answered too; ``off`` is 404 and the route leaves the OpenAPI
document).

It reads stored rows only (the scheduler leader writes them), so any API
process answers, with no scheduler and no LLM credential. Concurrent polls
share one read: a result is reused for 5 seconds, and the read itself runs in
a read-only transaction under a 10 second budget. The response is never
cached by a client or a proxy (``Cache-Control: no-store``).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from config.settings import get_settings
from db.base import get_db
from services.operations.status import OperationsStatusV1, read_operations_status

router = APIRouter(prefix="/admin/operations", tags=["admin-operations"])

# How long a computed document is reused for concurrent or rapid polls.
REUSE_SECONDS = 5.0


class _SingleFlight:
    """One in-process read at a time; its result is reused for a short window.

    The lock is created per event loop (a test client may run several loops
    in one process).
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._last: tuple[float, OperationsStatusV1] | None = None

    def _lock(self) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        lock = self._locks.get(loop_id)
        if lock is None:
            self._locks = {loop_id: asyncio.Lock()}
            lock = self._locks[loop_id]
        return lock

    def _fresh(self) -> OperationsStatusV1 | None:
        if self._last is not None and time.monotonic() - self._last[0] < REUSE_SECONDS:
            return self._last[1]
        return None

    async def get(self, db: AsyncSession) -> OperationsStatusV1:
        cached = self._fresh()
        if cached is not None:
            return cached
        async with self._lock():
            cached = self._fresh()
            if cached is not None:
                return cached
            document = await read_operations_status(
                db, now=datetime.now(UTC), settings=get_settings()
            )
            self._last = (time.monotonic(), document)
            return document

    def clear(self) -> None:
        """Forget the reused result (tests)."""
        self._last = None


_single_flight = _SingleFlight()


@router.get(
    "/status",
    response_model=OperationsStatusV1,
    summary="Scheduled jobs, work queue backlogs and platform LLM lanes",
    responses={
        200: {"description": "The operations status document (aggregates only)."},
    },
)
async def get_operations_status(
    admin: AdminUser,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> OperationsStatusV1:
    """The state of operations, in aggregates only.

    - ``jobs``: one entry per scheduled job with its last start, finish and
      outcome (``ok`` / ``failed`` / ``missed``), last success, consecutive
      failures, the exception class name of a failure, the counts the run
      returned, and ``overdue`` (no run started within the expected interval
      plus ``OPERATIONS_STATUS_OVERDUE_GRACE_SECONDS``).
    - ``backlogs``: count and oldest item per work queue.
    - ``llm``: per configured platform lane, the provider and model, the last
      successful platform-paid call and the call counts of the last hour and
      day, and the latest probe when ``OPERATIONS_PROBE_INTERVAL_SECONDS`` is
      set.

    No user, workspace or context identifier, address, content or exception
    message is part of the response.
    """
    response.headers["Cache-Control"] = "no-store"
    return await _single_flight.get(db)
