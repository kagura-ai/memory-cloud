"""Erase identity references in retained workspace history (#2739)."""

from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.operation_history import OperationHistory

# Columns that hold an account id on every row: the actor, the affected member
# and the recorded creator of a private context.
ERASED_COLUMNS = ("actor_user_id", "subject_user_id", "context_owner_id")
# ``target_id`` holds an account id only on membership events; on every other
# target type it identifies a resource or credential and is left alone.
MEMBER_TARGET_TYPE = "member"


async def erase_operation_history(db: AsyncSession, user_id: str, pseudonym: str) -> int:
    """Scrub actors, affected members and historical private-context owners.

    No copied names/emails/opaque request metadata exist here. Names are joined
    from the live account at read time. The caller owns the erasure transaction.
    """
    count = 0
    for field in ERASED_COLUMNS:
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
    # Membership events also use the member's account ID as their target.
    # Other target types identify resources/credentials and must not be rewritten.
    targets = cast(
        CursorResult[Any],
        await db.execute(
            update(OperationHistory)
            .where(
                OperationHistory.target_type == MEMBER_TARGET_TYPE,
                OperationHistory.target_id == user_id,
            )
            .values(target_id=pseudonym)
            .execution_options(synchronize_session=False)
        ),
    )
    count += targets.rowcount or 0
    return count
