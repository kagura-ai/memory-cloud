"""The total memory cap refusal names what the caller can review (#3022).

``QuotaService.check_memory_quota`` refuses a write at the workspace's
effective memory limit with a typed ``QuotaExceededError`` (``quota_type:
"memories"``, the gate block and ``remedies``). ``MemoryService.remember``
puts the target context on that refusal (:func:`set_memory_cap_target`), and
each surface that answers it (MCP ``remember``, ``remember_batch``,
``update_memory``'s create half, REST ``POST /memory/remember``) adds the
caller's never-delivered count for that context, read by
:func:`refusal_never_delivered_count`.

The count is the one the list filter and the context Overview card show
(``services.memory_listing.count_never_delivered``: the caller's own rows, the
private-context owner filter, the agent binding), so the number in the
refusal is the ``total`` of the caller's next ``list`` with
``never_delivered``. It is read only on the refusal path, after the refused
transaction was rolled back (the workspace row lock is released), with a
client-side and a server-side time limit; a failure or a slow read leaves it
out and never changes the refusal itself.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select

from services.memory_listing import count_never_delivered
from services.plan_headroom import MEMORIES, never_delivered_sentence
from utils.exceptions import QuotaExceededError
from utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

# The count is advice on a refusal: it gives up after this long (client side)
# and the statement after the same time on the server.
COUNT_TIMEOUT_SECONDS = 1.0

_TARGET_ATTR = "_memory_cap_target"


@dataclass(frozen=True)
class MemoryCapTarget:
    """The context a refused write was for, read while its row was loaded."""

    context_id: UUID
    is_private: bool


def is_memory_cap(exc: BaseException) -> bool:
    """Whether ``exc`` is the typed total memory cap refusal."""
    return isinstance(exc, QuotaExceededError) and exc.details.get("quota_type") == MEMORIES


def set_memory_cap_target(exc: QuotaExceededError, target: MemoryCapTarget) -> None:
    """Record the target context on a total-cap refusal (not part of its details)."""
    setattr(exc, _TARGET_ATTR, target)


def memory_cap_target(exc: BaseException) -> MemoryCapTarget | None:
    """The target recorded by :func:`set_memory_cap_target`, if any."""
    target = getattr(exc, _TARGET_ATTR, None)
    return target if isinstance(target, MemoryCapTarget) else None


async def _count(db: AsyncSession, user_id: str, target: MemoryCapTarget) -> int:
    # Transaction-local: ends with the rollback below, so a cancelled wait
    # cannot leave the statement running past the limit.
    timeout_ms = str(int(COUNT_TIMEOUT_SECONDS * 1000))
    await db.execute(select(func.set_config("statement_timeout", timeout_ms, True)))
    count = await count_never_delivered(
        db,
        context_id=target.context_id,
        owner_user_id=user_id if target.is_private else None,
        viewer_user_id=user_id,
    )
    await db.rollback()  # read only; ends the transaction and its timeout
    return count


async def refusal_never_delivered_count(
    db: AsyncSession, exc: BaseException, *, user_id: str
) -> int | None:
    """The caller's never-delivered count in the refused write's target context.

    Call after the refused transaction was rolled back. ``None`` when ``exc``
    names no target, or the read failed or took longer than
    ``COUNT_TIMEOUT_SECONDS`` (logged as ``memory_cap_refusal_count_unavailable``);
    the session is rolled back then, so the caller can go on with it.

    Args:
        db: The surface's session (no transaction of its own left open).
        exc: The total-cap refusal.
        user_id: The caller.
    """
    target = memory_cap_target(exc)
    if target is None:
        return None
    try:
        return await asyncio.wait_for(_count(db, user_id, target), timeout=COUNT_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning(
            "memory_cap_refusal_count_unavailable",
            context_id=str(target.context_id),
            error_class=type(e).__name__,
        )
        with contextlib.suppress(Exception):
            await db.rollback()
        return None


def refusal_message(message: str, count: int | None) -> str:
    """``message`` with the count sentence appended when the count is above 0.

    The quota message ends in the plan parenthesis without a full stop, so
    one is added before the count sentence (never a second one).
    """
    if not count:
        return message
    base = message if message.endswith(".") else f"{message}."
    return f"{base} {never_delivered_sentence(count)}"


async def memory_cap_fields(
    db: AsyncSession, exc: BaseException, *, user_id: str
) -> dict[str, Any]:
    """``never_delivered_count`` (when read) and ``remedies`` for a refusal reply."""
    count = await refusal_never_delivered_count(db, exc, user_id=user_id)
    fields: dict[str, Any] = {}
    remedies = exc.details.get("remedies") if isinstance(exc, QuotaExceededError) else None
    if isinstance(remedies, list):
        fields["remedies"] = list(remedies)
    if count is not None:
        fields["never_delivered_count"] = count
    return fields
