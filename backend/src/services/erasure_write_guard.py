"""Hold the live author before quota/context locks; reject stale erased auth."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User
from services.erasure_details import erasure_pseudonym, erasure_request_pseudonyms
from utils.exceptions import NotFoundException


async def hold_remember_author(db: AsyncSession, user_id: str) -> None:
    # FOR KEY SHARE conflicts with erasure's user FOR UPDATE/DELETE. Inserts
    # finish before erasure captures UUID scopes, or see the user gone.
    live = await db.scalar(
        select(User.id).where(User.user_id == user_id).with_for_update(read=True, key_share=True)
    )
    if live is not None:
        return
    # Existing internal/non-user writers remain supported. A pseudonym is an
    # intentional workspace-content author; it must never be mistaken for the
    # raw subject whose hash happens to have the same 64-hex shape.
    subjects = frozenset((await erasure_request_pseudonyms(db)).values())
    erased = erasure_pseudonym(user_id) in subjects
    if erased:
        raise NotFoundException("User")


async def lock_merge_contexts(db: AsyncSession, context_ids: Sequence[UUID]) -> None:
    from models.auth import Context

    # NO KEY UPDATE lets erasure's child FK checks take KEY SHARE while a
    # merge waits for its source rows. Contexts always precede memory locks.
    await db.execute(
        select(Context)
        .where(Context.id.in_(context_ids))
        .order_by(Context.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
