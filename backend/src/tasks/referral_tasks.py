"""Apply the referral grants a busy workspace row kept pending (#2563).

``ReferralService.apply_pending_grants`` takes the user's oldest live owned
workspace row ``NOWAIT`` (#2443). When that row stays busy past the tries the
grants stay pending: ``create_workspace`` swallows the ``ConflictError``, and
the user may never create another workspace. This job is the retry point:
every ``REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_SECONDS`` (default 300; 0 turns
it off) it takes up to ``REFERRAL_PENDING_GRANT_SWEEP_BATCH`` users who have a
pending grant and own a live workspace, oldest grant first, and applies each
user's grants in a transaction of its own. A user whose workspace row is still
busy is left for the next run. ``GET /referrals/me`` applies them as well.

Lock order: each apply starts with no row lock held — no ``users`` row — and
takes one workspace row ``NOWAIT`` and then, ``SKIP LOCKED``, the user's grant
rows (whose foreign-key check takes a KEY SHARE on that workspace row), so the
job never waits on a row lock. A grant row another transaction holds (an
account erasure's cascade) is left for the next run (#2602).

No single_flight: the scheduler leader runs one copy and APScheduler does not
start a run while the last one is still running. A copy in another process
would only apply the same grants again: an applied grant no longer matches,
and the bonus is recomputed as an absolute sum.

Not gated on ``ENABLE_REFERRALS``, like the apply in ``create_workspace``:
it mints nothing, it credits grants already earned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import (
    REFERRAL_PENDING_GRANT_SWEEP_BATCH,
    REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_DEFAULT,
    REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_ENV,
)
from db.base import get_db
from services.referral_service import ReferralService
from tasks.env import env_interval_seconds
from utils.exceptions import ConflictError
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PendingGrantSweep:
    """What one sweep did.

    Attributes:
        candidates: Users selected (pending grant, live owned workspace).
        users_applied: Users whose grants were applied.
        grants_applied: Grant rows re-homed.
        busy: Users whose workspace row stayed busy past the tries.
        failed: Users whose apply raised anything else.
    """

    candidates: int = 0
    users_applied: int = 0
    grants_applied: int = 0
    busy: int = 0
    failed: int = 0


async def sweep_pending_referral_grants(
    db: AsyncSession, *, limit: int = REFERRAL_PENDING_GRANT_SWEEP_BATCH
) -> PendingGrantSweep:
    """Apply the pending grants of up to ``limit`` users, one transaction each.

    Args:
        db: The session the sweep runs in.
        limit: The most users to apply.

    Returns:
        The counts of the run.
    """
    service = ReferralService(db)
    user_ids = await service.users_with_pending_grants(limit=limit)
    # The selection's transaction ends here, so each apply starts its own with
    # nothing locked.
    await db.rollback()

    users_applied = grants_applied = busy = failed = 0
    for user_id in user_ids:
        try:
            applied = await service.apply_pending_grants(user_id)
        except ConflictError:
            busy += 1
            logger.info("referral_pending_grant_sweep_busy", user_id=user_id)
        except Exception as exc:
            failed += 1
            logger.warning(
                "referral_pending_grant_sweep_user_failed",
                user_id=user_id,
                error_class=type(exc).__name__,
                exc_info=True,
            )
        else:
            if applied:
                users_applied += 1
                grants_applied += applied
        finally:
            # Ends this user's transaction: the apply committed what it applied,
            # and a busy or failed one leaves nothing for the next user.
            await db.rollback()

    return PendingGrantSweep(
        candidates=len(user_ids),
        users_applied=users_applied,
        grants_applied=grants_applied,
        busy=busy,
        failed=failed,
    )


async def apply_pending_referral_grants_task() -> None:
    """The scheduled job: one sweep, logged when it found anyone. Never raises."""
    try:
        async for db in get_db():
            result = await sweep_pending_referral_grants(db)
            if result.candidates:
                logger.info("referral_pending_grant_sweep_done", **asdict(result))
    except Exception as exc:
        logger.error(
            "referral_pending_grant_sweep_failed", error_class=type(exc).__name__, exc_info=True
        )


def schedule_referral_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the pending-grant sweep, unless its interval is 0.

    Args:
        scheduler: APScheduler instance.
    """
    interval = env_interval_seconds(
        REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_ENV, REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_DEFAULT
    )
    if interval <= 0:
        logger.info("referral_pending_grant_sweep_disabled", reason="interval_zero")
        return
    scheduler.add_job(
        apply_pending_referral_grants_task,
        trigger=IntervalTrigger(seconds=interval),
        id="apply_pending_referral_grants",
        name="Apply Pending Referral Grants",
        replace_existing=True,
    )
    logger.info("scheduled_referral_pending_grant_sweep", interval_seconds=interval)
