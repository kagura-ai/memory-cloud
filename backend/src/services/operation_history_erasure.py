"""Erase identity references in retained workspace history (#2739)."""

from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.operation_history import OperationHistory


async def erase_operation_history(db: AsyncSession, user_id: str, pseudonym: str) -> int:
    """Scrub actors, affected members and historical private-context owners.

    No copied names/emails/opaque request metadata exist here. Names are joined
    from the live account at read time. The caller owns the erasure transaction.
    """
    count = 0
    for field in ("actor_user_id", "subject_user_id", "context_owner_id"):
        result = cast(
            CursorResult[Any],
            await db.execute(
                update(OperationHistory)
                .where(getattr(OperationHistory, field) == user_id)
                .values({field: pseudonym})
                .execution_options(synchronize_session=False)
            ),
        )
        count += result.rowcount or 0
    return count
