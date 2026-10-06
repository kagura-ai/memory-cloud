"""#1784: identity links — one person's accounts counted as one owner.

Real-DB tests: the rule is a set of SQL predicates plus the access checks
built on them. They need a live Postgres (``db_session`` skips otherwise)
and run in CI's integration job.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.memory import recall
from config.settings import get_settings
from models.auth import (
    AuditLog,
    Context,
    IdentityLink,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from models.memory import Memory
from models.schemas import RecallRequest, RecallResponse
from services.context_service import ContextService
from services.identity_link_service import (
    MAX_LINKED_IDENTITIES,
    IdentityLinkService,
    is_same_owner,
    linked_user_ids,
    opens_contexts_as_self,
    owned_by,
)
from services.permission_service import PermissionService
from services.tag_resolution import fetch_vocabulary
from utils.datetime import utcnow
from utils.exceptions import AuthorizationError, ConflictError, NotFoundException, ValidationError
from utils.hashing import hmac_sha256_hex


async def _user(db: AsyncSession, prefix: str = "u") -> User:
    uid = f"{prefix}_{uuid4().hex[:10]}"
    user = User(
        user_id=uid,
        email=f"{uid}@link.example",
        name=uid,
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
    )
    db.add(user)
    await db.flush()
    return user


async def _workspace(db: AsyncSession, owner: User, *members: User) -> Workspace:
    workspace = Workspace(id=uuid4(), name="Link Test", owner_user_id=owner.user_id)
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=owner.user_id, role=WorkspaceRole.OWNER)
    )
    for member in members:
        db.add(
            WorkspaceMember(
                workspace_id=workspace.id, user_id=member.user_id, role=WorkspaceRole.ADMIN
            )
        )
    await db.flush()
    return workspace


async def _private_context(db: AsyncSession, workspace: Workspace, creator: User) -> Context:
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"priv_{uuid4().hex[:8]}",
        display_name="Private",
        created_by=creator.user_id,
        is_private=True,
    )
    db.add(context)
    await db.flush()
    return context


class TestLinkSets:
    @pytest.mark.asyncio
    async def test_an_unlinked_account_is_only_itself(self, db_session):
        user = await _user(db_session)

        assert await linked_user_ids(db_session, user.user_id) == {user.user_id}
        assert await is_same_owner(db_session, user.user_id, user.user_id)
        assert not await is_same_owner(db_session, user.user_id, "someone-else")
        assert not await is_same_owner(db_session, user.user_id, None)

    @pytest.mark.asyncio
    async def test_link_is_symmetric_and_idempotent(self, db_session):
        admin, oauth = await _user(db_session, "local"), await _user(db_session, "google")
        service = IdentityLinkService(db_session)

        both = {admin.user_id, oauth.user_id}
        assert await service.link(admin.user_id, oauth.user_id) == both
        # A repeat changes nothing and says so.
        assert await service.link(oauth.user_id, admin.user_id) == frozenset()

        assert await linked_user_ids(db_session, admin.user_id) == both
        assert await linked_user_ids(db_session, oauth.user_id) == both
        assert await is_same_owner(db_session, admin.user_id, oauth.user_id)
        assert await is_same_owner(db_session, oauth.user_id, admin.user_id)
        rows = await db_session.execute(select(IdentityLink).where(IdentityLink.user_id.in_(both)))
        assert len(rows.scalars().all()) == 2

    @pytest.mark.asyncio
    async def test_a_third_account_joins_the_existing_set(self, db_session):
        a, b, c = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)

        await service.link(a.user_id, b.user_id)
        await service.link(c.user_id, a.user_id)

        assert await linked_user_ids(db_session, b.user_id) == {a.user_id, b.user_id, c.user_id}

    @pytest.mark.asyncio
    async def test_two_sets_become_one(self, db_session):
        a, b, c, d = [await _user(db_session) for _ in range(4)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)
        await service.link(c.user_id, d.user_id)

        await service.link(b.user_id, c.user_id)

        everyone = {a.user_id, b.user_id, c.user_id, d.user_id}
        assert await linked_user_ids(db_session, d.user_id) == everyone

    @pytest.mark.asyncio
    async def test_a_set_cannot_grow_past_the_cap(self, db_session):
        users = [await _user(db_session) for _ in range(MAX_LINKED_IDENTITIES + 1)]
        service = IdentityLinkService(db_session)
        for other in users[1:MAX_LINKED_IDENTITIES]:
            await service.link(users[0].user_id, other.user_id)

        with pytest.raises(ConflictError):
            await service.link(users[0].user_id, users[-1].user_id)

        assert users[-1].user_id not in await linked_user_ids(db_session, users[0].user_id)

    @pytest.mark.asyncio
    async def test_self_link_and_unknown_account_are_refused(self, db_session):
        user = await _user(db_session)
        service = IdentityLinkService(db_session)

        with pytest.raises(ValidationError):
            await service.link(user.user_id, user.user_id)
        with pytest.raises(NotFoundException):
            await service.link(user.user_id, "no-such-account")

    @pytest.mark.asyncio
    async def test_unlink_dissolves_a_pair(self, db_session):
        a, b = await _user(db_session), await _user(db_session)
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)

        await service.unlink(b.user_id, a.user_id)

        assert await linked_user_ids(db_session, a.user_id) == {a.user_id}
        rows = await db_session.execute(
            select(IdentityLink).where(IdentityLink.user_id.in_([a.user_id, b.user_id]))
        )
        assert rows.scalars().all() == []

    @pytest.mark.asyncio
    async def test_unlink_leaves_the_rest_of_a_larger_set(self, db_session):
        a, b, c = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)
        await service.link(a.user_id, c.user_id)

        await service.unlink(a.user_id, c.user_id)

        assert await linked_user_ids(db_session, a.user_id) == {a.user_id, b.user_id}
        assert await linked_user_ids(db_session, c.user_id) == {c.user_id}

    @pytest.mark.asyncio
    async def test_unlink_in_a_larger_set_is_audited_on_every_account_it_separates(
        self, db_session
    ):
        a, b, c = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)
        await service.link(a.user_id, c.user_id)

        former = await service.unlink(a.user_id, c.user_id)

        assert former == frozenset({a.user_id, b.user_id})
        rows = await db_session.execute(
            select(AuditLog.user_id).where(
                AuditLog.user_id.in_([a.user_id, b.user_id, c.user_id]),
                AuditLog.action == "identity_unlinked",
            )
        )
        assert sorted(r.user_id for r in rows) == sorted(
            [a.user_id, b.user_id, c.user_id, c.user_id]
        )

    @pytest.mark.asyncio
    async def test_unlinking_a_stranger_is_not_found(self, db_session):
        a, b, stranger = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)

        with pytest.raises(NotFoundException):
            await service.unlink(a.user_id, stranger.user_id)
        with pytest.raises(NotFoundException):
            await service.unlink(stranger.user_id, a.user_id)

        assert await is_same_owner(db_session, a.user_id, b.user_id)

    @pytest.mark.asyncio
    async def test_list_linked_names_the_others_only(self, db_session):
        a, b = await _user(db_session), await _user(db_session)
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)

        listed = await service.list_linked(a.user_id)

        assert [(item.user_id, item.email) for item in listed] == [(b.user_id, b.email)]

    @pytest.mark.asyncio
    async def test_link_and_unlink_are_audited_on_both_accounts(self, db_session):
        a, b = await _user(db_session), await _user(db_session)
        service = IdentityLinkService(db_session)

        await service.link(a.user_id, b.user_id, ip_address="203.0.113.7")
        await service.unlink(a.user_id, b.user_id)

        rows = await db_session.execute(
            select(AuditLog.user_id, AuditLog.action, AuditLog.new_value_hash).where(
                AuditLog.user_id.in_([a.user_id, b.user_id])
            )
        )
        seen = {(r.user_id, r.action) for r in rows}
        assert seen == {
            (a.user_id, "identity_linked"),
            (b.user_id, "identity_linked"),
            (a.user_id, "identity_unlinked"),
            (b.user_id, "identity_unlinked"),
        }

    @staticmethod
    async def _linked_audit(db_session, users: list[User]) -> set[tuple[str, str]]:
        """``(trail, hashed other account)`` of every ``identity_linked`` row."""
        rows = await db_session.execute(
            select(AuditLog.user_id, AuditLog.new_value_hash).where(
                AuditLog.user_id.in_([u.user_id for u in users]),
                AuditLog.action == "identity_linked",
            )
        )
        return {(r.user_id, r.new_value_hash) for r in rows}

    @staticmethod
    def _pairs(left: list[User], right: list[User]) -> set[tuple[str, str]]:
        """Both directions of every pair across ``left`` and ``right``."""
        key = get_settings().audit_hmac_key
        return {
            (actor.user_id, hmac_sha256_hex(target.user_id, key))
            for one in left
            for other in right
            for actor, target in ((one, other), (other, one))
        }

    @pytest.mark.asyncio
    async def test_merging_two_sets_is_audited_on_every_cross_pair(self, db_session):
        """#1875: X and Y gain co-owners too, so their trails say so."""
        a, x, b, y = [await _user(db_session) for _ in range(4)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, x.user_id)
        await service.link(b.user_id, y.user_id)
        before = await self._linked_audit(db_session, [a, x, b, y])

        affected = await service.link(a.user_id, b.user_id)

        assert affected == {a.user_id, x.user_id, b.user_id, y.user_id}
        added = await self._linked_audit(db_session, [a, x, b, y]) - before
        assert added == self._pairs([a, x], [b, y])

    @pytest.mark.asyncio
    async def test_linking_a_set_to_a_single_account_is_audited_for_every_member(self, db_session):
        a, x, b = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, x.user_id)
        before = await self._linked_audit(db_session, [a, x, b])

        affected = await service.link(a.user_id, b.user_id)

        assert affected == {a.user_id, x.user_id, b.user_id}
        added = await self._linked_audit(db_session, [a, x, b]) - before
        assert added == self._pairs([a, x], [b])

    @pytest.mark.asyncio
    async def test_the_single_account_may_be_the_one_naming_the_set(self, db_session):
        """Same result whichever side the caller is on."""
        a, x, b = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, x.user_id)
        before = await self._linked_audit(db_session, [a, x, b])

        affected = await service.link(b.user_id, a.user_id)

        assert affected == {a.user_id, x.user_id, b.user_id}
        added = await self._linked_audit(db_session, [a, x, b]) - before
        assert added == self._pairs([b], [a, x])

    @pytest.mark.asyncio
    async def test_deleting_an_account_removes_it_from_its_set(self, db_session):
        a, b = await _user(db_session), await _user(db_session)
        await IdentityLinkService(db_session).link(a.user_id, b.user_id)

        await db_session.delete(b)
        await db_session.commit()

        assert await linked_user_ids(db_session, a.user_id) == {a.user_id}


class TestLeavingASet:
    """#1807: an account leaves its set and the others stay linked."""

    @pytest.mark.asyncio
    async def test_leaving_a_set_of_three_keeps_the_other_two_linked(self, db_session):
        a, b, c = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)
        await service.link(a.user_id, c.user_id)

        left = await service.leave(a.user_id)

        assert left == frozenset({b.user_id, c.user_id})
        assert await linked_user_ids(db_session, a.user_id) == {a.user_id}
        assert await linked_user_ids(db_session, b.user_id) == {b.user_id, c.user_id}
        assert await is_same_owner(db_session, c.user_id, b.user_id)

    @pytest.mark.asyncio
    async def test_leaving_a_pair_dissolves_it(self, db_session):
        a, b = await _user(db_session), await _user(db_session)
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)

        assert await service.leave(b.user_id) == frozenset({a.user_id})

        rows = await db_session.execute(
            select(IdentityLink).where(IdentityLink.user_id.in_([a.user_id, b.user_id]))
        )
        assert rows.scalars().all() == []

    @pytest.mark.asyncio
    async def test_an_account_in_no_set_has_nothing_to_leave(self, db_session):
        lone = await _user(db_session)

        with pytest.raises(NotFoundException):
            await IdentityLinkService(db_session).leave(lone.user_id)

    @pytest.mark.asyncio
    async def test_leaving_is_audited_on_every_account_of_the_set(self, db_session):
        a, b, c = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)
        await service.link(a.user_id, c.user_id)

        await service.leave(a.user_id)

        rows = await db_session.execute(
            select(AuditLog.user_id).where(
                AuditLog.user_id.in_([a.user_id, b.user_id, c.user_id]),
                AuditLog.action == "identity_unlinked",
            )
        )
        # The leaver once per former partner; each partner once.
        assert sorted(r.user_id for r in rows) == sorted(
            [a.user_id, a.user_id, b.user_id, c.user_id]
        )

    @pytest.mark.asyncio
    async def test_the_leaver_no_longer_names_the_others_rows(self, db_session):
        a, b, c = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)
        await service.link(a.user_id, c.user_id)

        await service.leave(a.user_id)

        rows = (
            (
                await db_session.execute(
                    select(IdentityLink).where(IdentityLink.user_id.in_([b.user_id, c.user_id]))
                )
            )
            .scalars()
            .all()
        )
        assert all(row.linked_by is None for row in rows)


class TestLinkedByFollowsTheAccount:
    """#1807: ``linked_by`` is a foreign key with ``ON DELETE SET NULL``."""

    @pytest.mark.asyncio
    async def test_deleting_the_linking_account_keeps_the_others_linked(self, db_session):
        a, b, c = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)
        await service.link(a.user_id, c.user_id)

        # A delete that skips the hand-over step (the delete_admin CLI).
        await db_session.delete(a)
        await db_session.commit()

        rows = (
            (
                await db_session.execute(
                    select(IdentityLink)
                    .where(IdentityLink.user_id.in_([b.user_id, c.user_id]))
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        assert [row.linked_by for row in rows] == [None, None]
        assert await linked_user_ids(db_session, b.user_id) == {b.user_id, c.user_id}


class TestPrivateContextAcrossLinkedAccounts:
    @pytest.mark.asyncio
    async def test_owned_by_matches_the_linked_creator(self, db_session):
        admin, oauth, stranger = [await _user(db_session) for _ in range(3)]
        workspace = await _workspace(db_session, admin, oauth, stranger)
        context = await _private_context(db_session, workspace, admin)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        async def sees(user: User) -> bool:
            row = await db_session.execute(
                select(Context.id).where(
                    Context.id == context.id, owned_by(Context.created_by, user.user_id)
                )
            )
            return row.scalar_one_or_none() is not None

        assert await sees(admin)
        assert await sees(oauth)
        assert not await sees(stranger)

    @pytest.mark.asyncio
    async def test_linked_account_opens_and_lists_the_private_context(self, db_session):
        admin, oauth = await _user(db_session, "local"), await _user(db_session, "google")
        workspace = await _workspace(db_session, admin, oauth)
        context = await _private_context(db_session, workspace, admin)
        permissions = PermissionService(db_session)

        with pytest.raises(AuthorizationError):
            await permissions.check_context_access(oauth.user_id, context.id)
        with pytest.raises(NotFoundException):
            await ContextService(db_session).get_context(oauth.user_id, context.id)

        await IdentityLinkService(db_session).link(oauth.user_id, admin.user_id)

        _, role = await permissions.check_context_access(oauth.user_id, context.id)
        assert role.value == "owner"
        opened = await ContextService(db_session).get_context(oauth.user_id, context.id)
        assert opened.id == context.id
        resolved = await permissions.resolve_context_for_workspace_read(oauth.user_id, context.id)
        assert resolved.id == context.id
        listed = await permissions.get_accessible_contexts(oauth.user_id, workspace.id)
        assert context.id in {c.id for c in listed}

    @pytest.mark.asyncio
    async def test_unlink_closes_it_again_at_once(self, db_session):
        admin, oauth = await _user(db_session), await _user(db_session)
        workspace = await _workspace(db_session, admin, oauth)
        context = await _private_context(db_session, workspace, admin)
        service = IdentityLinkService(db_session)
        await service.link(admin.user_id, oauth.user_id)

        await service.unlink(admin.user_id, oauth.user_id)

        with pytest.raises(AuthorizationError):
            await PermissionService(db_session).check_context_access(oauth.user_id, context.id)
        listed = await PermissionService(db_session).get_accessible_contexts(
            oauth.user_id, workspace.id
        )
        assert context.id not in {c.id for c in listed}

    @pytest.mark.asyncio
    async def test_a_link_does_not_carry_workspace_membership(self, db_session):
        """The linked account is not a member of the creator's workspace: the
        link widens ownership, never membership."""
        admin, outsider = await _user(db_session), await _user(db_session)
        workspace = await _workspace(db_session, admin)
        context = await _private_context(db_session, workspace, admin)
        await IdentityLinkService(db_session).link(admin.user_id, outsider.user_id)
        permissions = PermissionService(db_session)

        with pytest.raises(AuthorizationError):
            await permissions.check_context_access(outsider.user_id, context.id)
        with pytest.raises(NotFoundException):
            await permissions.resolve_context_for_workspace_read(outsider.user_id, context.id)
        with pytest.raises(NotFoundException):
            await ContextService(db_session).get_context(outsider.user_id, context.id)

    @pytest.mark.asyncio
    async def test_a_third_party_still_sees_nothing(self, db_session):
        admin, oauth, stranger = [await _user(db_session) for _ in range(3)]
        workspace = await _workspace(db_session, admin, oauth, stranger)
        context = await _private_context(db_session, workspace, admin)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)
        permissions = PermissionService(db_session)

        with pytest.raises(AuthorizationError):
            await permissions.check_context_access(stranger.user_id, context.id)
        listed = await permissions.get_accessible_contexts(stranger.user_id, workspace.id)
        assert context.id not in {c.id for c in listed}


async def _memory(db: AsyncSession, context: Context, author: User) -> Memory:
    memory = Memory(
        id=uuid4(),
        user_id=author.user_id,
        workspace_id=context.workspace_id,
        context_id=context.id,
        summary="a summary long enough",
        content="content",
        type="note",
        client="pytest",
        embedding_status="success",
    )
    db.add(memory)
    await db.flush()
    return memory


class TestMemoriesInAPrivateContextAcrossLinkedAccounts:
    """The admin's private context, written to by both of the person's
    accounts. After a link each account reads all of it; before, and for
    anyone else, the single-author rule holds."""

    @staticmethod
    async def _seed(db):
        admin, oauth, stranger = [await _user(db) for _ in range(3)]
        workspace = await _workspace(db, admin, oauth, stranger)
        context = await _private_context(db, workspace, admin)
        by_admin = await _memory(db, context, admin)
        by_oauth = await _memory(db, context, oauth)
        return admin, oauth, stranger, workspace, context, by_admin, by_oauth

    @staticmethod
    async def _visible(db, context: Context, viewer: User) -> set:
        rows = await db.execute(
            select(Memory.id).where(
                Memory.context_id == context.id, owned_by(Memory.user_id, viewer.user_id)
            )
        )
        return set(rows.scalars().all())

    @pytest.mark.asyncio
    async def test_owner_filter_covers_both_authors_once_linked(self, db_session):
        admin, oauth, stranger, _, context, by_admin, by_oauth = await self._seed(db_session)

        assert await self._visible(db_session, context, admin) == {by_admin.id}

        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        both = {by_admin.id, by_oauth.id}
        assert await self._visible(db_session, context, admin) == both
        assert await self._visible(db_session, context, oauth) == both
        assert await self._visible(db_session, context, stranger) == set()

    @pytest.mark.asyncio
    async def test_can_access_memory_follows_the_link(self, db_session):
        admin, oauth, stranger, workspace, context, by_admin, by_oauth = await self._seed(
            db_session
        )
        permissions = PermissionService(db_session)

        async def can(viewer: User, memory: Memory) -> bool:
            return await permissions.can_access_memory(
                user_id=viewer.user_id,
                memory_user_id=memory.user_id,
                workspace_id=workspace.id,
                context_id=context.id,
            )

        assert not await can(admin, by_oauth)
        assert not await can(oauth, by_admin)

        await IdentityLinkService(db_session).link(oauth.user_id, admin.user_id)

        assert await can(admin, by_oauth)
        assert await can(oauth, by_admin)
        assert not await can(stranger, by_admin)
        assert not await can(stranger, by_oauth)

    @pytest.mark.asyncio
    async def test_a_linked_author_does_not_open_someone_elses_private_context(self, db_session):
        """Both conditions must hold: the memory's author is the caller's, and
        so is the context. A link to the author alone is not enough."""
        admin, oauth, stranger, workspace, _, _, _ = await self._seed(db_session)
        strangers_context = await _private_context(db_session, workspace, stranger)
        written_by_oauth = await _memory(db_session, strangers_context, oauth)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        allowed = await PermissionService(db_session).can_access_memory(
            user_id=admin.user_id,
            memory_user_id=written_by_oauth.user_id,
            workspace_id=workspace.id,
            context_id=strangers_context.id,
        )

        assert not allowed

    @pytest.mark.asyncio
    async def test_export_includes_the_linked_accounts_memories(self, db_session):
        admin, oauth, _, _, context, by_admin, by_oauth = await self._seed(db_session)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        exported = await ContextService(db_session).export_context(oauth.user_id, context.id)

        assert {str(m.id) for m in exported.memories} == {str(by_admin.id), str(by_oauth.id)}

    @pytest.mark.asyncio
    async def test_unlink_hides_the_other_accounts_memories_again(self, db_session):
        admin, oauth, _, _, context, by_admin, _ = await self._seed(db_session)
        service = IdentityLinkService(db_session)
        await service.link(admin.user_id, oauth.user_id)

        await service.unlink(admin.user_id, oauth.user_id)

        assert await self._visible(db_session, context, admin) == {by_admin.id}


class TestAuthMeCarriesTheLinkedIds:
    """The web UI attributes a linked account's private contexts to the viewer."""

    @pytest.mark.asyncio
    async def test_me_lists_the_other_accounts_only(self, db_session):
        from api.routes.auth import get_current_user_info

        admin, oauth = await _user(db_session), await _user(db_session)

        before = await get_current_user_info(user={"user_id": admin.user_id}, db=db_session)
        assert before["user"]["linked_user_ids"] == []

        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        as_admin = await get_current_user_info(user={"user_id": admin.user_id}, db=db_session)
        as_oauth = await get_current_user_info(user={"user_id": oauth.user_id}, db=db_session)
        assert as_admin["user"]["linked_user_ids"] == [oauth.user_id]
        assert as_oauth["user"]["linked_user_ids"] == [admin.user_id]


async def _member(
    db: AsyncSession, workspace: Workspace, user: User, role: WorkspaceRole, allowed=None
) -> None:
    db.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.user_id,
            role=role,
            allowed_context_ids=allowed,
        )
    )
    await db.flush()


class TestALinkDoesNotLiftTheCallersOwnLimits:
    """The linked account is checked as itself: its role, its whitelist."""

    @staticmethod
    async def _seed(db, role: WorkspaceRole, *, allowed_self: bool | None):
        admin, oauth = await _user(db), await _user(db)
        workspace = await _workspace(db, admin)
        context = await _private_context(db, workspace, admin)
        allowed = None if allowed_self is None else ([context.id] if allowed_self else [])
        await _member(db, workspace, oauth, role, allowed)
        await IdentityLinkService(db).link(admin.user_id, oauth.user_id)
        return oauth, context

    @pytest.mark.asyncio
    async def test_a_viewer_reads_but_does_not_own(self, db_session):
        oauth, context = await self._seed(db_session, WorkspaceRole.VIEWER, allowed_self=None)
        permissions = PermissionService(db_session)

        _, role = await permissions.check_context_access(
            oauth.user_id, context.id, required_role="viewer"
        )
        assert role.value == "viewer"
        with pytest.raises(AuthorizationError):
            await permissions.check_context_write(oauth.user_id, context.id)
        with pytest.raises(AuthorizationError):
            await permissions.check_context_owner(oauth.user_id, context.id)

    @pytest.mark.asyncio
    async def test_a_member_whose_whitelist_names_the_context_owns_it(self, db_session):
        oauth, context = await self._seed(db_session, WorkspaceRole.MEMBER, allowed_self=True)

        _, role = await PermissionService(db_session).check_context_access(
            oauth.user_id, context.id
        )

        assert role.value == "owner"

    @pytest.mark.asyncio
    async def test_a_member_whose_whitelist_omits_the_context_is_refused(self, db_session):
        oauth, context = await self._seed(db_session, WorkspaceRole.MEMBER, allowed_self=False)

        with pytest.raises(AuthorizationError):
            await PermissionService(db_session).check_context_access(oauth.user_id, context.id)

    @pytest.mark.asyncio
    async def test_a_suspended_member_is_refused(self, db_session):
        """MEMBER with no whitelist at all (Migration 042)."""
        oauth, context = await self._seed(db_session, WorkspaceRole.MEMBER, allowed_self=None)
        permissions = PermissionService(db_session)

        with pytest.raises(AuthorizationError):
            await permissions.check_context_access(oauth.user_id, context.id)
        with pytest.raises(NotFoundException):
            await permissions.resolve_context_for_workspace_read(oauth.user_id, context.id)
        with pytest.raises(NotFoundException):
            await ContextService(db_session).get_context(oauth.user_id, context.id)


class TestRestRecallChecksTheLinkedAccountAsItself:
    """``POST /memory/recall`` resolves ``filters.context_id`` for the caller
    before anything is searched: the linked account reaches the creator's
    private context only where its own membership admits it. Every refusal is
    the uniform 404 and the search never runs."""

    @staticmethod
    def _request(context: Context) -> RecallRequest:
        return RecallRequest(query="what did I write", k=5, filters={"context_id": str(context.id)})

    @staticmethod
    async def _recall(db, caller: User, context: Context, *, workspace_id):
        service = AsyncMock()
        service.recall = AsyncMock(return_value=RecallResponse(results=[]))
        response = await recall(
            request=TestRestRecallChecksTheLinkedAccountAsItself._request(context),
            user={"user_id": caller.user_id, "current_workspace_id": workspace_id},
            memory_service=service,
            db=db,
        )
        return response, service

    @pytest.mark.asyncio
    async def test_a_member_whose_whitelist_names_the_context_recalls_it(self, db_session):
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.MEMBER, allowed_self=True
        )

        _, service = await self._recall(
            db_session, oauth, context, workspace_id=context.workspace_id
        )

        kwargs = service.recall.await_args.kwargs
        assert kwargs["current_context_id"] == context.id
        assert kwargs["context_workspace_id"] == context.workspace_id

    @pytest.mark.asyncio
    async def test_a_member_whose_whitelist_excludes_the_context_gets_404(self, db_session):
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.MEMBER, allowed_self=False
        )

        with pytest.raises(NotFoundException):
            await self._recall(db_session, oauth, context, workspace_id=context.workspace_id)

    @pytest.mark.asyncio
    async def test_a_suspended_member_gets_404(self, db_session):
        """MEMBER with a NULL whitelist (Migration 042)."""
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.MEMBER, allowed_self=None
        )

        with pytest.raises(NotFoundException):
            await self._recall(db_session, oauth, context, workspace_id=context.workspace_id)

    @pytest.mark.asyncio
    async def test_an_account_removed_from_the_workspace_gets_404(self, db_session):
        """Even while the workspace is still the account's current one: the
        route never trusted ``current_workspace_id`` for this."""
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.ADMIN, allowed_self=None
        )
        await db_session.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == context.workspace_id,
                WorkspaceMember.user_id == oauth.user_id,
            )
        )

        with pytest.raises(NotFoundException):
            await self._recall(db_session, oauth, context, workspace_id=context.workspace_id)

    @pytest.mark.asyncio
    async def test_the_search_is_never_run_for_a_refused_caller(self, db_session):
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.MEMBER, allowed_self=False
        )
        service = AsyncMock()
        service.recall = AsyncMock(return_value=RecallResponse(results=[]))

        with pytest.raises(NotFoundException):
            await recall(
                request=self._request(context),
                user={"user_id": oauth.user_id, "current_workspace_id": context.workspace_id},
                memory_service=service,
                db=db_session,
            )

        service.recall.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_caller_with_no_current_workspace_recalls_a_context_it_can_open(
        self, db_session
    ):
        """After a member removal or a workspace deletion cleared the account's
        current workspace, a context it can open elsewhere is still searched —
        in that context's workspace — rather than refused until the next
        sign-in."""
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.MEMBER, allowed_self=True
        )

        _, service = await self._recall(db_session, oauth, context, workspace_id=None)

        kwargs = service.recall.await_args.kwargs
        assert kwargs["current_workspace_id"] == context.workspace_id
        assert kwargs["context_workspace_id"] == context.workspace_id

    @pytest.mark.asyncio
    async def test_a_caller_with_no_current_workspace_is_still_refused_elsewhere(self, db_session):
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.MEMBER, allowed_self=False
        )

        with pytest.raises(NotFoundException):
            await self._recall(db_session, oauth, context, workspace_id=None)


class TestTheReadPathsWidenToTheLinkSetOnlyAsAMember:
    """Defence in depth behind the route / handler gate: ``SearchService`` and
    the tag vocabulary widen a private read to the link set only when the
    caller, checked as itself, can open the context — the rule
    ``resolve_context_for_workspace_read`` applies (#1874 spelled it for
    memory health). Otherwise the caller reads its own rows only."""

    @staticmethod
    async def _opens(db, caller: User, context: Context) -> bool:
        return await opens_contexts_as_self(
            db, caller.user_id, workspace_id=context.workspace_id, context_ids=[context.id]
        )

    @pytest.mark.asyncio
    async def test_owner_admin_and_unrestricted_viewer_open_every_context(self, db_session):
        for role in (WorkspaceRole.ADMIN, WorkspaceRole.VIEWER):
            oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
                db_session, role, allowed_self=None
            )
            assert await self._opens(db_session, oauth, context), role
        admin = await _user(db_session)
        workspace = await _workspace(db_session, admin)
        context = await _private_context(db_session, workspace, admin)
        assert await self._opens(db_session, admin, context)

    @pytest.mark.asyncio
    async def test_a_whitelist_admits_exactly_what_it_names(self, db_session):
        for role in (WorkspaceRole.MEMBER, WorkspaceRole.VIEWER):
            oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
                db_session, role, allowed_self=True
            )
            assert await self._opens(db_session, oauth, context), role
            oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
                db_session, role, allowed_self=False
            )
            assert not await self._opens(db_session, oauth, context), role

    @pytest.mark.asyncio
    async def test_every_context_of_a_cross_context_read_must_be_named(self, db_session):
        admin, oauth = await _user(db_session), await _user(db_session)
        workspace = await _workspace(db_session, admin)
        named = await _private_context(db_session, workspace, admin)
        unnamed = await _private_context(db_session, workspace, admin)
        await _member(db_session, workspace, oauth, WorkspaceRole.MEMBER, [named.id])

        assert await opens_contexts_as_self(
            db_session, oauth.user_id, workspace_id=workspace.id, context_ids=[named.id]
        )
        assert not await opens_contexts_as_self(
            db_session,
            oauth.user_id,
            workspace_id=workspace.id,
            context_ids=[named.id, unnamed.id],
        )

    @pytest.mark.asyncio
    async def test_a_suspended_member_a_non_member_and_a_deleted_workspace_are_refused(
        self, db_session
    ):
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.MEMBER, allowed_self=None
        )
        assert not await self._opens(db_session, oauth, context)

        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.ADMIN, allowed_self=None
        )
        await db_session.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == context.workspace_id,
                WorkspaceMember.user_id == oauth.user_id,
            )
        )
        assert not await self._opens(db_session, oauth, context)

        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.ADMIN, allowed_self=None
        )
        workspace = await db_session.get(Workspace, context.workspace_id)
        assert workspace is not None
        workspace.deleted_at = utcnow()
        await db_session.flush()
        assert not await self._opens(db_session, oauth, context)

    @pytest.mark.asyncio
    async def test_the_private_vocabulary_of_a_refused_linked_account_is_its_own(self, db_session):
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.MEMBER, allowed_self=False
        )
        creator = (
            await db_session.execute(select(User).where(User.user_id == context.created_by))
        ).scalar_one()
        theirs = await _memory(db_session, context, creator)
        theirs.tags = ["creator-only"]
        mine = await _memory(db_session, context, oauth)
        mine.tags = ["mine"]
        await db_session.flush()

        vocabulary = await fetch_vocabulary(
            db_session,
            workspace_id=context.workspace_id,
            context_id=context.id,
            user_id=oauth.user_id,
        )

        assert vocabulary == {"mine": 1}

    @pytest.mark.asyncio
    async def test_the_private_vocabulary_of_an_admitted_linked_account_covers_the_set(
        self, db_session
    ):
        oauth, context = await TestALinkDoesNotLiftTheCallersOwnLimits._seed(
            db_session, WorkspaceRole.MEMBER, allowed_self=True
        )
        creator = (
            await db_session.execute(select(User).where(User.user_id == context.created_by))
        ).scalar_one()
        theirs = await _memory(db_session, context, creator)
        theirs.tags = ["creator-only"]
        await db_session.flush()

        vocabulary = await fetch_vocabulary(
            db_session,
            workspace_id=context.workspace_id,
            context_id=context.id,
            user_id=oauth.user_id,
        )

        assert vocabulary == {"creator-only": 1}


class TestALinkNeverCrossesWorkspaces:
    @pytest.mark.asyncio
    async def test_the_unscoped_memory_list_stays_the_callers_own(self, db_session):
        """``GET /memory/list`` with no context has no workspace predicate:
        widening it would hand over the linked account's memories from
        workspaces the caller is not a member of."""
        from api.routes.memory import list_memories

        admin, oauth = await _user(db_session), await _user(db_session)
        elsewhere = await _workspace(db_session, admin)
        context = await _private_context(db_session, elsewhere, admin)
        theirs = await _memory(db_session, context, admin)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        listed = await list_memories(
            user={"user_id": oauth.user_id},
            db=db_session,
            scope=None,
            type=None,
            context_id=None,
            q=None,
            tags=None,
            tags_match="any",
            trigger_from=None,
            trigger_until=None,
            lat_min=None,
            lat_max=None,
            lon_min=None,
            lon_max=None,
            order_by="created_at",
            limit=50,
            offset=0,
        )

        ids = {str(m.id) for m in listed.memories}
        assert str(theirs.id) not in ids

    @pytest.mark.asyncio
    async def test_unscoped_stats_stay_the_callers_own(self, db_session):
        from services.memory_service import MemoryService

        admin, oauth = await _user(db_session), await _user(db_session)
        elsewhere = await _workspace(db_session, admin)
        context = await _private_context(db_session, elsewhere, admin)
        await _memory(db_session, context, admin)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        stats = await MemoryService(db_session).get_stats(
            user_id=oauth.user_id, include_details=False
        )

        assert stats.total_count == 0


class TestErasingOneLinkedAccount:
    """The survivor keeps what it wrote in the other account's private context."""

    @pytest.mark.asyncio
    async def test_private_contexts_pass_to_the_linked_member(self, db_session):
        from services.identity_link_service import hand_over_private_contexts

        admin, oauth = await _user(db_session), await _user(db_session)
        workspace = await _workspace(db_session, admin, oauth)
        context = await _private_context(db_session, workspace, admin)
        mine = await _memory(db_session, context, oauth)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        own = await _memory(db_session, context, admin)
        own_id = own.id

        counts = await hand_over_private_contexts(db_session, admin.user_id)
        await db_session.flush()

        assert counts == {"contexts_handed_over": 1, "identity_links_removed": 2}
        assert context.created_by == oauth.user_id
        # The leaving account's own memories in that context are deleted: the
        # new owner could otherwise publish them by making the context shared.
        gone = await db_session.execute(select(Memory.id).where(Memory.id == own_id))
        assert gone.scalar_one_or_none() is None
        assert await linked_user_ids(db_session, oauth.user_id) == {oauth.user_id}
        # The survivor reads its own memory through its own id from here on.
        _, role = await PermissionService(db_session).check_context_access(
            oauth.user_id, context.id
        )
        assert role.value == "owner"
        assert await PermissionService(db_session).can_access_memory(
            user_id=oauth.user_id,
            memory_user_id=mine.user_id,
            workspace_id=workspace.id,
            context_id=context.id,
        )

    @pytest.mark.asyncio
    async def test_a_context_with_no_linked_member_in_its_workspace_is_left_alone(self, db_session):
        from services.identity_link_service import hand_over_private_contexts

        admin, oauth = await _user(db_session), await _user(db_session)
        workspace = await _workspace(db_session, admin)  # oauth is not a member
        context = await _private_context(db_session, workspace, admin)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        counts = await hand_over_private_contexts(db_session, admin.user_id)

        assert counts["contexts_handed_over"] == 0
        assert context.created_by == admin.user_id

    @pytest.mark.asyncio
    async def test_an_unlinked_account_is_a_no_op(self, db_session):
        from services.identity_link_service import hand_over_private_contexts

        user = await _user(db_session)

        assert await hand_over_private_contexts(db_session, user.user_id) == {
            "contexts_handed_over": 0,
            "identity_links_removed": 0,
        }

    @pytest.mark.asyncio
    async def test_linked_by_does_not_keep_the_erased_id_in_a_larger_set(self, db_session):
        from services.identity_link_service import hand_over_private_contexts

        a, b, c = [await _user(db_session) for _ in range(3)]
        service = IdentityLinkService(db_session)
        await service.link(a.user_id, b.user_id)
        await service.link(a.user_id, c.user_id)

        await hand_over_private_contexts(db_session, a.user_id)
        await db_session.flush()

        rows = (
            (
                await db_session.execute(
                    select(IdentityLink).where(IdentityLink.user_id.in_([b.user_id, c.user_id]))
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(row.linked_by is None for row in rows)
        assert await linked_user_ids(db_session, b.user_id) == {b.user_id, c.user_id}

    @pytest.mark.asyncio
    async def test_a_context_the_survivor_never_wrote_in_is_left_alone(self, db_session):
        """Nothing of the survivor's to keep: the context is the caller's to
        handle as for any erased account."""
        from services.identity_link_service import hand_over_private_contexts

        admin, oauth = await _user(db_session), await _user(db_session)
        workspace = await _workspace(db_session, admin, oauth)
        context = await _private_context(db_session, workspace, admin)
        await _memory(db_session, context, admin)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        counts = await hand_over_private_contexts(db_session, admin.user_id)

        assert counts["contexts_handed_over"] == 0
        assert context.created_by == admin.user_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("role", "whitelisted", "inherits"),
        [
            (WorkspaceRole.VIEWER, None, False),
            (WorkspaceRole.MEMBER, False, False),
            (WorkspaceRole.MEMBER, True, True),
        ],
    )
    async def test_the_heir_must_be_able_to_own_the_context_as_a_linked_account(
        self, db_session, role, whitelisted, inherits
    ):
        """The hand-over never gives an account more than the link did."""
        from services.identity_link_service import hand_over_private_contexts

        admin, oauth = await _user(db_session), await _user(db_session)
        workspace = await _workspace(db_session, admin)
        context = await _private_context(db_session, workspace, admin)
        allowed = None if whitelisted is None else ([context.id] if whitelisted else [])
        await _member(db_session, workspace, oauth, role, allowed)
        await _memory(db_session, context, oauth)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        counts = await hand_over_private_contexts(db_session, admin.user_id)

        assert counts["contexts_handed_over"] == (1 if inherits else 0)
        assert context.created_by == (oauth.user_id if inherits else admin.user_id)


class TestWritesByMemoryId:
    """PATCH and forget address a memory by id and rely on ``can_access_memory``."""

    @pytest.mark.asyncio
    async def test_a_linked_viewer_reads_but_cannot_write(self, db_session):
        admin, oauth = await _user(db_session), await _user(db_session)
        workspace = await _workspace(db_session, admin)
        context = await _private_context(db_session, workspace, admin)
        memory = await _memory(db_session, context, admin)
        await _member(db_session, workspace, oauth, WorkspaceRole.VIEWER)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)
        permissions = PermissionService(db_session)

        async def can(access: str) -> bool:
            return await permissions.can_access_memory(
                user_id=oauth.user_id,
                memory_user_id=memory.user_id,
                workspace_id=workspace.id,
                context_id=context.id,
                access=access,
            )

        assert await can("read")
        assert not await can("write")

    @pytest.mark.asyncio
    async def test_a_linked_admin_writes(self, db_session):
        admin, oauth = await _user(db_session), await _user(db_session)
        workspace = await _workspace(db_session, admin, oauth)
        context = await _private_context(db_session, workspace, admin)
        memory = await _memory(db_session, context, admin)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        assert await PermissionService(db_session).can_access_memory(
            user_id=oauth.user_id,
            memory_user_id=memory.user_id,
            workspace_id=workspace.id,
            context_id=context.id,
            access="write",
        )


class TestTheReadSurfacesInsideALinkedPrivateContext:
    """Each widened call site, driven through its own entry point."""

    @staticmethod
    async def _seed(db):
        admin, oauth = await _user(db), await _user(db)
        workspace = await _workspace(db, admin, oauth)
        context = await _private_context(db, workspace, admin)
        by_admin = await _memory(db, context, admin)
        by_oauth = await _memory(db, context, oauth)
        await IdentityLinkService(db).link(admin.user_id, oauth.user_id)
        return admin, oauth, workspace, context, by_admin, by_oauth

    @pytest.mark.asyncio
    async def test_the_memory_list_of_the_context_shows_both_authors(self, db_session):
        from api.routes.memory import list_memories

        _, oauth, _, context, by_admin, by_oauth = await self._seed(db_session)

        listed = await list_memories(
            user={"user_id": oauth.user_id},
            db=db_session,
            scope=None,
            type=None,
            context_id=context.id,
            q=None,
            tags=None,
            tags_match="any",
            trigger_from=None,
            trigger_until=None,
            lat_min=None,
            lat_max=None,
            lon_min=None,
            lon_max=None,
            order_by="created_at",
            limit=50,
            offset=0,
        )

        assert {str(m.id) for m in listed.memories} == {str(by_admin.id), str(by_oauth.id)}

    @pytest.mark.asyncio
    async def test_stats_of_the_context_count_both_authors(self, db_session):
        from services.memory_service import MemoryService

        _, oauth, workspace, context, _, _ = await self._seed(db_session)

        stats = await MemoryService(db_session).get_stats(
            user_id=oauth.user_id,
            workspace_id=str(workspace.id),
            context_id=str(context.id),
            include_details=False,
        )

        assert stats.total_count == 2

    @pytest.mark.asyncio
    async def test_access_patterns_of_the_context_cover_both_authors(self, db_session):
        """#1807: the access patterns of a private context follow the link set,
        as its memory list and stats do."""
        from api.routes.memory import get_access_patterns

        _, oauth, _, context, by_admin, by_oauth = await self._seed(db_session)
        for memory in (by_admin, by_oauth):
            memory.access_count = 3
            memory.last_used_at = utcnow()
        await db_session.commit()

        patterns = await get_access_patterns(
            user={"user_id": oauth.user_id}, context_id=context.id, db=db_session, days=30
        )

        assert {m["memory_id"] for m in patterns["most_accessed"]} == {
            str(by_admin.id),
            str(by_oauth.id),
        }
        assert sum(patterns["type_distribution"].values()) == 2

    @pytest.mark.asyncio
    async def test_access_patterns_of_a_shared_context_stay_the_callers_own(self, db_session):
        from api.routes.memory import get_access_patterns

        _, oauth, _, context, by_admin, by_oauth = await self._seed(db_session)
        context.is_private = False
        for memory in (by_admin, by_oauth):
            memory.access_count = 3
            memory.last_used_at = utcnow()
        await db_session.commit()

        patterns = await get_access_patterns(
            user={"user_id": oauth.user_id}, context_id=context.id, db=db_session, days=30
        )

        assert [m["memory_id"] for m in patterns["most_accessed"]] == [str(by_oauth.id)]


class TestErasingThroughTheService:
    """``AccountErasureService`` runs the hand-over before it pseudonymizes
    ``created_by`` — the wiring, not only the helper."""

    @pytest.mark.asyncio
    async def test_the_postgres_sweep_hands_the_context_over(self, db_session):
        from services.account_erasure_service import AccountErasureService

        admin, oauth, third = [await _user(db_session) for _ in range(3)]
        workspace = await _workspace(db_session, third, admin, oauth)
        context = await _private_context(db_session, workspace, admin)
        mine = await _memory(db_session, context, oauth)
        await IdentityLinkService(db_session).link(admin.user_id, oauth.user_id)

        counts = await AccountErasureService(db_session)._delete_postgres(admin)
        await db_session.flush()
        await db_session.refresh(context)

        assert counts["contexts_handed_over"] == 1
        assert context.created_by == oauth.user_id
        assert await linked_user_ids(db_session, oauth.user_id) == {oauth.user_id}
        permissions = PermissionService(db_session)
        _, role = await permissions.check_context_access(oauth.user_id, context.id)
        assert role.value == "owner"
        assert await permissions.can_access_memory(
            user_id=oauth.user_id,
            memory_user_id=mine.user_id,
            workspace_id=workspace.id,
            context_id=context.id,
        )
        listed = await permissions.get_accessible_contexts(third.user_id, workspace.id)
        assert context.id not in {c.id for c in listed}


class TestConcurrentLinks:
    @pytest.mark.asyncio
    async def test_the_cap_holds_under_concurrent_links(self, db_session, async_engine):
        """Five links into one set at once: the set ends at the cap, in one group."""
        import asyncio

        hub = await _user(db_session)
        others = [await _user(db_session) for _ in range(5)]
        await db_session.commit()

        async def link(other: User) -> bool:
            async with AsyncSession(async_engine, expire_on_commit=False) as session:
                try:
                    await IdentityLinkService(session).link(hub.user_id, other.user_id)
                    return True
                except ConflictError:
                    await session.rollback()
                    return False

        results = await asyncio.gather(*(link(o) for o in others))

        assert sum(results) == MAX_LINKED_IDENTITIES - 1
        members = await linked_user_ids(db_session, hub.user_id)
        assert len(members) == MAX_LINKED_IDENTITIES
        groups = await db_session.execute(
            select(IdentityLink.group_id).where(IdentityLink.user_id.in_(members)).distinct()
        )
        assert len(groups.scalars().all()) == 1
