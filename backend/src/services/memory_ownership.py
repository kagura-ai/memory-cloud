"""Ownership predicates shared by memory writes and declared graph links."""

from uuid import UUID

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context
from models.memory import Memory
from services.identity_link_service import owned_by


async def is_private_context(db: AsyncSession, context_id: UUID | str | None) -> bool:
    """Whether a live context explicitly allows linked-account ownership.

    Access treats a missing, deleted or NULL context conservatively as private,
    but here True widens ownership to the link set. Unclear contexts therefore
    stay on the caller's own rows.
    """
    if context_id is None:
        return False
    context_uuid = context_id if isinstance(context_id, UUID) else UUID(str(context_id))
    result = await db.execute(
        select(Context.is_private).where(
            Context.id == context_uuid,
            Context.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none() is True


async def own_memory_predicate(
    db: AsyncSession, context_id: UUID | str | None, user_id: str
) -> ColumnElement[bool]:
    """Match memories the caller may name as their own (#1803, #2313).

    Only a private context includes linked accounts; a shared or unknown
    context keeps the caller's own memories. Callers still constrain the query
    to the authorized workspace/context and live rows.
    """
    if await is_private_context(db, context_id):
        return owned_by(Memory.user_id, user_id)
    return Memory.user_id == user_id
