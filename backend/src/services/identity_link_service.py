"""Identity links (#1784): one person's accounts counted as one owner.

Identities are keyed by ``user_id`` and never merged by email (#481). A CLI
admin (``local:<login>``) and an OAuth account that belong to one person are
therefore two ``users`` rows, and everything compared against a single id —
who created a private context, who wrote a memory in it — treats the
person's own work as someone else's.

A link set fixes that for ownership, and only for ownership:

* a private context is open to every account linked to its creator;
* inside it, memories written by any of those accounts are visible.

Roles, workspace membership, ``allowed_context_ids`` and an API key's
workspace scope stay the caller's own. A link never lets an account reach a
workspace it is not a member of.

Linking needs proof of both accounts and never an email match: the target
must be signed in on the same browser session as the caller (the multi-account
session container, #1488), which each account entered through its own
sign-in.

The lookups here are the single place the rule is spelled:

* SQL filters use :func:`owned_by` — a subquery, no extra round trip;
* Python checks use :func:`is_same_owner` — one query, and only when the
  plain ``==`` has already failed;
* the vector filter needs the ids themselves: :func:`linked_user_ids`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from config.settings import get_settings
from models.auth import AuditLog, IdentityLink, User
from utils.exceptions import ConflictError, NotFoundException, ValidationError
from utils.hashing import hmac_sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

# One person, a handful of sign-ins. The cap keeps a set from growing into a
# way of sharing private contexts between people.
MAX_LINKED_IDENTITIES = 4

# Links and unlinks run one at a time, deployment-wide. They are rare, and a
# set is read and rewritten as a whole: two concurrent links into one set
# could each pass the size cap, and an unlink could dissolve a set another
# link had just joined.
_LINK_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(
    key="identity_links"
)


def _forget_cached_tags() -> None:
    """Drop the tag-vocabulary cache, which is keyed by one user id and would
    otherwise keep serving a former link's tags until its TTL."""
    from services.tag_resolution import clear_vocabulary_cache

    clear_vocabulary_cache()


def linked_ids_subquery(user_id: str) -> Any:
    """``SELECT user_id`` of every account linked to ``user_id`` (itself included
    when it is linked at all). Empty for an account with no links."""
    mine = aliased(IdentityLink)
    other = aliased(IdentityLink)
    return (
        select(other.user_id)
        .where(other.group_id.in_(select(mine.group_id).where(mine.user_id == user_id)))
        .scalar_subquery()
    )


def owned_by(column: Any, user_id: str) -> ColumnElement[bool]:
    """SQL predicate: ``column`` holds ``user_id`` or an account linked to it."""
    return or_(column == user_id, column.in_(linked_ids_subquery(user_id)))


async def linked_user_ids(db: AsyncSession, user_id: str) -> frozenset[str]:
    """``user_id`` plus every account linked to it."""
    rows = await db.execute(
        select(IdentityLink.user_id).where(owned_by(IdentityLink.user_id, user_id))
    )
    return frozenset({user_id, *rows.scalars().all()})


async def is_same_owner(db: AsyncSession, user_id: str, other_user_id: str | None) -> bool:
    """Whether ``other_user_id`` is ``user_id`` or an account linked to it.

    Equal ids answer without a query, so the common case — the caller's own
    context — costs nothing.
    """
    if other_user_id is None:
        return False
    if user_id == other_user_id:
        return True
    row = await db.execute(
        select(IdentityLink.user_id)
        .where(
            IdentityLink.user_id == other_user_id,
            IdentityLink.user_id.in_(linked_ids_subquery(user_id)),
        )
        .limit(1)
    )
    return row.scalar_one_or_none() == other_user_id


@dataclass(frozen=True)
class LinkedIdentity:
    """One account in the caller's link set, as shown on the profile page."""

    user_id: str
    email: str | None
    name: str | None
    linked_at: datetime | None


class IdentityLinkService:
    """Create, remove and list identity links. Commits its own writes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_linked(self, user_id: str) -> list[LinkedIdentity]:
        """The other accounts in ``user_id``'s link set (never itself)."""
        rows = await self.db.execute(
            select(IdentityLink.user_id, IdentityLink.linked_at, User.email, User.name)
            .join(User, User.user_id == IdentityLink.user_id)
            .where(
                IdentityLink.user_id.in_(linked_ids_subquery(user_id)),
                IdentityLink.user_id != user_id,
            )
            .order_by(IdentityLink.linked_at, IdentityLink.user_id)
        )
        return [
            LinkedIdentity(user_id=r.user_id, email=r.email, name=r.name, linked_at=r.linked_at)
            for r in rows
        ]

    async def link(
        self,
        user_id: str,
        other_user_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Put ``user_id`` and ``other_user_id`` in one link set.

        The caller has already proved both accounts (same browser session).
        Idempotent for a pair that is already linked.

        Raises:
            ValidationError: An account cannot be linked to itself.
            NotFoundException: Either account no longer exists.
            ConflictError: The resulting set would exceed
                ``MAX_LINKED_IDENTITIES``.
        """
        if user_id == other_user_id:
            raise ValidationError("An account cannot be linked to itself")

        await self.db.execute(_LINK_LOCK_SQL)
        # Both rows are locked too, in a fixed order: an account being erased
        # or deleted is seen here, not after the link is written.
        users = {
            u.user_id: u
            for u in (
                await self.db.execute(
                    select(User)
                    .where(User.user_id.in_([user_id, other_user_id]))
                    .order_by(User.user_id)
                    .with_for_update()
                )
            ).scalars()
        }
        if user_id not in users or other_user_id not in users:
            raise NotFoundException("Account")

        groups = {
            row.user_id: row.group_id
            for row in await self.db.execute(
                select(IdentityLink.user_id, IdentityLink.group_id).where(
                    IdentityLink.user_id.in_([user_id, other_user_id])
                )
            )
        }
        mine, theirs = groups.get(user_id), groups.get(other_user_id)
        if mine is not None and mine == theirs:
            return

        async def size(group_id: uuid.UUID | None) -> int:
            if group_id is None:
                return 1
            count = await self.db.execute(
                select(func.count())
                .select_from(IdentityLink)
                .where(IdentityLink.group_id == group_id)
            )
            return int(count.scalar_one())

        if await size(mine) + await size(theirs) > MAX_LINKED_IDENTITIES:
            raise ConflictError(f"At most {MAX_LINKED_IDENTITIES} accounts can be linked together")

        group_id = mine or theirs or uuid.uuid4()
        if mine is not None and theirs is not None:
            # Both already belong to a set: the two sets become one.
            await self.db.execute(
                update(IdentityLink)
                .where(IdentityLink.group_id == theirs)
                .values(group_id=group_id)
            )
        for member in (user_id, other_user_id):
            if member not in groups:
                self.db.add(IdentityLink(group_id=group_id, user_id=member, linked_by=user_id))

        self._audit(users[user_id], "identity_linked", other_user_id, ip_address, user_agent)
        self._audit(users[other_user_id], "identity_linked", user_id, ip_address, user_agent)
        await self.db.commit()
        _forget_cached_tags()
        logger.info("identity_linked", user_id=user_id, linked_user_id=other_user_id)

    async def unlink(
        self,
        user_id: str,
        other_user_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Take ``other_user_id`` out of ``user_id``'s link set.

        Either side can cut the link from its own session. A set left with one
        account is removed.

        Raises:
            NotFoundException: ``other_user_id`` is not linked to ``user_id``
                (also for an account that does not exist — the answer does
                not say which).
        """
        if user_id == other_user_id:
            raise NotFoundException("Linked account")
        await self.db.execute(_LINK_LOCK_SQL)
        rows = (
            (
                await self.db.execute(
                    select(IdentityLink)
                    .where(IdentityLink.user_id.in_(linked_ids_subquery(user_id)))
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        members = {row.user_id: row for row in rows}
        if other_user_id not in members or user_id not in members:
            raise NotFoundException("Linked account")

        group_id = members[user_id].group_id
        await self.db.execute(delete(IdentityLink).where(IdentityLink.user_id == other_user_id))
        if len(members) <= 2:
            await self.db.execute(delete(IdentityLink).where(IdentityLink.group_id == group_id))

        users = {
            u.user_id: u
            for u in (
                await self.db.execute(
                    select(User).where(User.user_id.in_([user_id, other_user_id]))
                )
            ).scalars()
        }
        for actor, target in ((user_id, other_user_id), (other_user_id, user_id)):
            if actor in users:
                self._audit(users[actor], "identity_unlinked", target, ip_address, user_agent)
        await self.db.commit()
        _forget_cached_tags()
        logger.info("identity_unlinked", user_id=user_id, unlinked_user_id=other_user_id)

    def _audit(
        self,
        user: User,
        action: str,
        other_user_id: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Stage one audit row on ``user``'s trail. The other account's id is
        HMAC-hashed, per the audit log's no-plaintext convention."""
        self.db.add(
            AuditLog(
                user_email=user.email,
                user_id=user.user_id,
                action=action,
                resource="identity_link",
                new_value_hash=hmac_sha256_hex(other_user_id, get_settings().audit_hmac_key),
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )


async def hand_over_private_contexts(db: AsyncSession, user_id: str) -> dict[str, int]:
    """Before ``user_id`` is erased or deleted: keep its link set's data reachable.

    A private context is readable through its creator. When the creator goes,
    a linked account that wrote memories in it would lose them along with the
    context. So each live private context ``user_id`` created passes to a
    linked account that is a member of the context's workspace — the same
    person, by the link. Contexts with no such account are left for the
    caller to handle as before.

    Then ``user_id`` leaves its set, a set left with one account is removed,
    and ``linked_by`` no longer names ``user_id`` on any row.

    Does not commit: it runs inside the caller's transaction.

    Returns:
        ``{"contexts_handed_over": n, "identity_links_removed": n}``.
    """
    from models.auth import Context, WorkspaceMember

    await db.execute(_LINK_LOCK_SQL)
    others = sorted((await linked_user_ids(db, user_id)) - {user_id})
    handed_over = 0
    if others:
        contexts = (
            (
                await db.execute(
                    select(Context).where(
                        Context.created_by == user_id,
                        Context.is_private.is_(True),
                        Context.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for context in contexts:
            heir = (
                await db.execute(
                    select(WorkspaceMember.user_id)
                    .where(
                        WorkspaceMember.workspace_id == context.workspace_id,
                        WorkspaceMember.user_id.in_(others),
                    )
                    .order_by(WorkspaceMember.user_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if heir is not None:
                context.created_by = heir
                handed_over += 1

    group_id = (
        await db.execute(select(IdentityLink.group_id).where(IdentityLink.user_id == user_id))
    ).scalar_one_or_none()
    removed = 0
    if group_id is not None:
        await db.execute(delete(IdentityLink).where(IdentityLink.user_id == user_id))
        removed = 1
        remaining = (
            (await db.execute(select(IdentityLink).where(IdentityLink.group_id == group_id)))
            .scalars()
            .all()
        )
        if len(remaining) <= 1:
            await db.execute(delete(IdentityLink).where(IdentityLink.group_id == group_id))
            removed += len(remaining)
        else:
            for row in remaining:
                if row.linked_by == user_id:
                    row.linked_by = row.user_id
    if handed_over or removed:
        _forget_cached_tags()
    return {"contexts_handed_over": handed_over, "identity_links_removed": removed}
