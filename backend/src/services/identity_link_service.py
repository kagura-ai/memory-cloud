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
sign-in, and both must have signed in there recently (#1803 — checked by the
route, ``IDENTITY_LINK_SIGN_IN_WINDOW``).

Inside a private context the link set is also the owner for writes that name
another memory (#1803): an ``external_id`` upsert and a remember's declared
links match a memory any linked account wrote there.

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


async def lock_identity_links(db: AsyncSession) -> None:
    """Serialize with every other link, unlink and hand-over until this
    transaction ends. Take it AFTER any ``users`` row lock."""
    await db.execute(_LINK_LOCK_SQL)


def _forget_cached_tags() -> None:
    """Drop this process's tag-vocabulary cache after a link change.

    Not needed for correctness: a private vocabulary is keyed by the link set
    itself (#1807), so after a change every API process reads a new key and
    never serves the former set's tags. This only frees the entries no one
    will read again in the process that handled the change."""
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
    ) -> bool:
        """Put ``user_id`` and ``other_user_id`` in one link set.

        The caller has already proved both accounts (same browser session,
        both signed in recently).
        Idempotent for a pair that is already linked.

        Returns:
            True when the link was created, False when the pair was already
            linked (nothing written, nothing to audit or notify).

        Raises:
            ValidationError: An account cannot be linked to itself.
            NotFoundException: Either account no longer exists.
            ConflictError: The resulting set would exceed
                ``MAX_LINKED_IDENTITIES``.
        """
        if user_id == other_user_id:
            raise ValidationError("An account cannot be linked to itself")

        # User rows first, then the link lock — the order account erasure
        # takes them in, so a link racing an erasure waits instead of
        # deadlocking. Both rows, in a fixed order: an account being erased
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
        await lock_identity_links(self.db)

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
            return False

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
        return True

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
        if user_id == other_user_id or not await is_same_owner(self.db, user_id, other_user_id):
            # Answered before the deployment-wide lock is taken: a caller
            # naming accounts it is not linked to never queues behind it.
            raise NotFoundException("Linked account")
        await lock_identity_links(self.db)
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

        await _remove_from_set(self.db, other_user_id, list(members.values()))

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

    async def leave(
        self,
        user_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> frozenset[str]:
        """Take ``user_id`` out of its own link set; the others stay linked.

        ``unlink`` takes one other account out of the caller's set, so an
        account in a set of three could only get out by unlinking both
        others — which also separated those two from each other (#1807).
        A set left with one account is removed.

        Returns:
            The accounts ``user_id`` was linked to until now.

        Raises:
            NotFoundException: ``user_id`` is not in a link set.
        """
        if await linked_user_ids(self.db, user_id) == {user_id}:
            # Answered before the deployment-wide lock is taken, as in unlink.
            raise NotFoundException("Identity link")
        await lock_identity_links(self.db)
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
        others = frozenset(row.user_id for row in rows) - {user_id}
        if len(rows) == len(others):
            # Left (or was unlinked) between the check above and the lock.
            raise NotFoundException("Identity link")
        await _remove_from_set(self.db, user_id, list(rows))

        users = {
            u.user_id: u
            for u in (
                await self.db.execute(select(User).where(User.user_id.in_([user_id, *others])))
            ).scalars()
        }
        for other in sorted(others):
            for actor, target in ((user_id, other), (other, user_id)):
                if actor in users:
                    self._audit(users[actor], "identity_unlinked", target, ip_address, user_agent)
        await self.db.commit()
        _forget_cached_tags()
        logger.info("identity_link_left", user_id=user_id, former_links=len(others))
        return others

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


async def _remove_from_set(db: AsyncSession, user_id: str, members: list[IdentityLink]) -> None:
    """Delete ``user_id``'s row from the set ``members`` (every row of it).

    A set left with one account is removed. Rows of the remaining accounts
    stop naming ``user_id`` in ``linked_by``: the account that made a link
    and has left the set is recorded as the row's own account, the same as
    the hand-over step writes. Runs under :func:`lock_identity_links`.
    """
    await db.execute(delete(IdentityLink).where(IdentityLink.user_id == user_id))
    remaining = [row for row in members if row.user_id != user_id]
    if len(remaining) <= 1:
        for row in remaining:
            await db.execute(delete(IdentityLink).where(IdentityLink.id == row.id))
        return
    for row in remaining:
        if row.linked_by == user_id:
            row.linked_by = row.user_id


async def hand_over_private_contexts(db: AsyncSession, user_id: str) -> dict[str, int]:
    """Before ``user_id`` is erased or deleted: keep its link set's data reachable.

    A private context is readable through its creator. When the creator goes,
    a linked account that wrote memories in it would lose them along with the
    context. So a live private context ``user_id`` created passes to a linked
    account when both hold:

    * a linked account wrote memories in it — otherwise there is nothing of
      the survivor's to keep, and the context is left for the caller to
      handle as before; and
    * that account could own it today as a linked account: a workspace owner
      or admin, or a member whose ``allowed_context_ids`` names the context.
      The hand-over never gives an account more than the link already did.

    The leaving account's own memories in a context that is handed over are
    deleted here. They are its private data, and the new owner could
    otherwise publish them by making the context shared.

    Then ``user_id`` leaves its set, a set left with one account is removed,
    and ``linked_by`` no longer names ``user_id`` on any row.

    Does not commit: it runs inside the caller's transaction, after the
    caller has locked the ``users`` row.

    Returns:
        ``{"contexts_handed_over": n, "identity_links_removed": n}``.
    """
    from auth.workspace_roles import WorkspaceRole
    from models.auth import Context, WorkspaceMember
    from models.memory import Memory

    await lock_identity_links(db)
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
            authors = set(
                (
                    await db.execute(
                        select(Memory.user_id)
                        .where(
                            Memory.context_id == context.id,
                            Memory.user_id.in_(others),
                            Memory.deleted_at.is_(None),
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            if not authors:
                continue
            members = (
                (
                    await db.execute(
                        select(WorkspaceMember).where(
                            WorkspaceMember.workspace_id == context.workspace_id,
                            WorkspaceMember.user_id.in_(authors),
                        )
                    )
                )
                .scalars()
                .all()
            )

            def rank(member: WorkspaceMember, context_id: uuid.UUID = context.id) -> int | None:
                """0 for a workspace owner/admin, 1 for a member whose whitelist
                names the context, None for an account that could not own it."""
                if member.role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
                    return 0
                if (
                    member.role == WorkspaceRole.MEMBER
                    and member.allowed_context_ids is not None
                    and context_id in member.allowed_context_ids
                ):
                    return 1
                return None

            eligible = sorted(
                ((rank(m), m.user_id) for m in members if rank(m) is not None),
            )
            if not eligible:
                continue
            await db.execute(
                delete(Memory).where(Memory.context_id == context.id, Memory.user_id == user_id)
            )
            context.created_by = eligible[0][1]
            handed_over += 1

    members = list(
        (
            await db.execute(
                select(IdentityLink).where(IdentityLink.user_id.in_(linked_ids_subquery(user_id)))
            )
        )
        .scalars()
        .all()
    )
    removed = 0
    if members:
        await _remove_from_set(db, user_id, members)
        removed = 1 if len(members) > 2 else len(members)
    if handed_over or removed:
        _forget_cached_tags()
    return {"contexts_handed_over": handed_over, "identity_links_removed": removed}
