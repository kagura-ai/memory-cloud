"""Tests for services/workspace_service.py.

Covers CRUD, member management, and statistics.
Heavy dependencies (ContextService, Qdrant) are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import Memory
from services.workspace_locks import lock_workspace_for_update
from services.workspace_service import WorkspaceService
from utils.datetime import utcnow
from utils.exceptions import NotFoundException, ValidationError

# ---------------------------------------------------------------------------
# validate_role
# ---------------------------------------------------------------------------


class TestValidateRole:
    @pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
    def test_valid_roles(self, role: str) -> None:
        assert WorkspaceService.validate_role(role) is None

    def test_invalid_role_raises(self) -> None:
        with pytest.raises(ValidationError, match="Invalid role"):
            WorkspaceService.validate_role("hacker")


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------


class TestCreateWorkspace:
    @pytest.mark.asyncio
    async def test_create_without_api_key_no_context(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = await service.create_workspace(
            name="Test WS",
            owner_user_id="u1",
            openai_api_key=None,
            create_default_context=False,
        )
        assert ws.name == "Test WS"
        assert ws.owner_user_id == "u1"
        assert ws.plan_name == "free"

    @pytest.mark.asyncio
    async def test_create_with_api_key_creates_context(self, db_session) -> None:
        service = WorkspaceService(db_session)
        mock_ctx = MagicMock()
        fake_context = MagicMock()
        fake_context.id = uuid4()
        mock_ctx.create_context = AsyncMock(return_value=fake_context)

        with (
            patch("services.context_service.ContextService", return_value=mock_ctx),
            patch("utils.encryption.get_encryptor") as mock_enc,
        ):
            mock_enc.return_value.encrypt.return_value = "encrypted"
            ws = await service.create_workspace(
                name="Test WS2",
                owner_user_id="u2",
                openai_api_key="sk-test",
            )
        assert ws.name == "Test WS2"
        mock_ctx.create_context.assert_called_once()


# ---------------------------------------------------------------------------
# get_workspace
# ---------------------------------------------------------------------------


class TestGetWorkspace:
    @pytest.mark.asyncio
    async def test_get_success(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W1", owner_user_id="u1", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        result = await service.get_workspace(ws.id)
        assert result.id == ws.id
        assert result.name == "W1"

    @pytest.mark.asyncio
    async def test_get_not_found(self, db_session) -> None:
        service = WorkspaceService(db_session)
        with pytest.raises(NotFoundException, match="Workspace not found"):
            await service.get_workspace(uuid4())


# ---------------------------------------------------------------------------
# list_user_workspaces
# ---------------------------------------------------------------------------


class TestListUserWorkspaces:
    @pytest.mark.asyncio
    async def test_empty(self, db_session) -> None:
        service = WorkspaceService(db_session)
        result = await service.list_user_workspaces("ghost_user")
        assert result == []

    @pytest.mark.asyncio
    async def test_lists_memberships(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W2", owner_user_id="u2", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        member = WorkspaceMember(workspace_id=ws.id, user_id="u2", role=WorkspaceRole.OWNER)
        db_session.add(member)
        await db_session.flush()

        result = await service.list_user_workspaces("u2")
        names = {w.name for w in result}
        assert "W2" in names


# ---------------------------------------------------------------------------
# update_workspace
# ---------------------------------------------------------------------------


class TestUpdateWorkspace:
    @pytest.mark.asyncio
    async def test_update_name_and_description(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="Old", owner_user_id="u3", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        updated = await service.update_workspace(ws.id, name="New", description="desc")
        assert updated.name == "New"
        assert updated.description == "desc"


# ---------------------------------------------------------------------------
# add_member
# ---------------------------------------------------------------------------


class TestAddMember:
    @pytest.mark.asyncio
    async def test_add_member_success(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W3", owner_user_id="u4", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        member = await service.add_member(ws.id, "u5", role=WorkspaceRole.MEMBER)
        assert member.user_id == "u5"
        assert member.role == "member"

    @pytest.mark.asyncio
    async def test_add_duplicate_raises(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W4", owner_user_id="u6", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        await service.add_member(ws.id, "u7", role=WorkspaceRole.MEMBER)
        with pytest.raises(ValidationError, match="already a member"):
            await service.add_member(ws.id, "u7", role=WorkspaceRole.MEMBER)


# ---------------------------------------------------------------------------
# get_member
# ---------------------------------------------------------------------------


class TestGetMember:
    @pytest.mark.asyncio
    async def test_get_member_success(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W5", owner_user_id="u8", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        member = WorkspaceMember(workspace_id=ws.id, user_id="u9", role=WorkspaceRole.ADMIN)
        db_session.add(member)
        await db_session.flush()

        result = await service.get_member(ws.id, "u9")
        assert result.role == "admin"

    @pytest.mark.asyncio
    async def test_get_member_not_found_raises(self, db_session) -> None:
        service = WorkspaceService(db_session)
        with pytest.raises(NotFoundException, match="Member not found"):
            await service.get_member(uuid4(), "ghost")

    @pytest.mark.asyncio
    async def test_get_member_not_found_no_raise(self, db_session) -> None:
        service = WorkspaceService(db_session)
        result = await service.get_member(uuid4(), "ghost", raise_if_not_found=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_member_with_lock(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W6", owner_user_id="u10", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        member = WorkspaceMember(workspace_id=ws.id, user_id="u11", role=WorkspaceRole.MEMBER)
        db_session.add(member)
        await db_session.flush()

        result = await service.get_member(ws.id, "u11", with_lock=True)
        assert result.role == "member"


# ---------------------------------------------------------------------------
# list_members
# ---------------------------------------------------------------------------


class TestListMembers:
    @pytest.mark.asyncio
    async def test_lists_all(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W7", owner_user_id="u12", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u12", role=WorkspaceRole.OWNER))
        db_session.add(
            WorkspaceMember(workspace_id=ws.id, user_id="u13", role=WorkspaceRole.MEMBER)
        )
        await db_session.flush()

        result = await service.list_members(ws.id)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# update_member_role
# ---------------------------------------------------------------------------


class TestUpdateMemberRole:
    @pytest.mark.asyncio
    async def test_promote_to_admin(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W8", owner_user_id="u14", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u14", role=WorkspaceRole.OWNER))
        db_session.add(
            WorkspaceMember(workspace_id=ws.id, user_id="u15", role=WorkspaceRole.MEMBER)
        )
        await db_session.flush()

        updated = await service.update_member_role(ws.id, "u15", "admin")
        assert updated.role == "admin"

    @pytest.mark.asyncio
    async def test_promote_to_owner_blocked(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W9", owner_user_id="u16", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u16", role=WorkspaceRole.OWNER))
        db_session.add(
            WorkspaceMember(workspace_id=ws.id, user_id="u17", role=WorkspaceRole.MEMBER)
        )
        await db_session.flush()

        with pytest.raises(ValidationError, match="already has an owner"):
            await service.update_member_role(ws.id, "u17", "owner")

    @pytest.mark.asyncio
    async def test_demote_owner_blocked(self, db_session) -> None:
        # #1108: demoting the owner via update_member_role would desync
        # owner_user_id (the SoT) from member.role — it would leave zero
        # OWNER-role rows while owner_user_id still names the demoted user.
        # Refuse here; owner changes must go through transfer_ownership
        # (symmetric to the promote-to-owner guard, same 'transfer first'
        # guidance).
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W10", owner_user_id="u18", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u18", role=WorkspaceRole.OWNER))
        await db_session.flush()

        with pytest.raises(ValidationError, match="transfer ownership"):
            await service.update_member_role(ws.id, "u18", "admin")

    @pytest.mark.asyncio
    async def test_demote_owner_preserves_owner_invariant(self, db_session) -> None:
        # The refused demotion must leave BOTH owner representations intact:
        # owner_user_id unchanged AND exactly one OWNER-role member remaining
        # (and it is still the owner_user_id holder). This is the desync the
        # #1102 lock did not by itself prevent.
        from sqlalchemy import select

        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W10b", owner_user_id="u19", plan_name="free")
        db_session.add(ws)
        await db_session.flush()
        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u19", role=WorkspaceRole.OWNER))
        await db_session.flush()

        with pytest.raises(ValidationError):
            await service.update_member_role(ws.id, "u19", "viewer")

        refreshed = await db_session.get(Workspace, ws.id)
        assert refreshed is not None
        assert refreshed.owner_user_id == "u19"

        owners = (
            (
                await db_session.execute(
                    select(WorkspaceMember).where(
                        WorkspaceMember.workspace_id == ws.id,
                        WorkspaceMember.role == WorkspaceRole.OWNER,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(owners) == 1
        assert owners[0].user_id == "u19"

    @pytest.mark.asyncio
    async def test_owner_change_takes_workspace_lock(self, db_session) -> None:
        # #1102: an owner-mutating change must serialize on the workspace row
        # lock. #1108: that change is now refused (demotion → transfer first),
        # but the lock is still acquired BEFORE the refusal, so the owner
        # invariant check runs under serialization.
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="WLk1", owner_user_id="o1", plan_name="free")
        db_session.add(ws)
        await db_session.flush()
        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="o1", role=WorkspaceRole.OWNER))
        await db_session.flush()

        with patch("services.workspace_service.lock_workspace_for_update", AsyncMock()) as lock:
            with pytest.raises(ValidationError, match="transfer ownership"):
                await service.update_member_role(ws.id, "o1", "admin")
        lock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_owner_change_skips_workspace_lock(self, db_session) -> None:
        # A plain member<->viewer/admin change never touches ownership → no lock,
        # keeping the common path contention-free.
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="WLk2", owner_user_id="o2", plan_name="free")
        db_session.add(ws)
        await db_session.flush()
        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="o2", role=WorkspaceRole.OWNER))
        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="m2", role=WorkspaceRole.MEMBER))
        await db_session.flush()

        with patch("services.workspace_service.lock_workspace_for_update", AsyncMock()) as lock:
            await service.update_member_role(ws.id, "m2", "admin")
        lock.assert_not_awaited()


# ---------------------------------------------------------------------------
# lock_workspace_for_update (#1102 shared owner-mutation lock)
# ---------------------------------------------------------------------------


class TestLockWorkspaceForUpdate:
    @pytest.mark.asyncio
    async def test_returns_live_workspace(self, db_session) -> None:
        ws = Workspace(id=uuid4(), name="WLock", owner_user_id="o", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        locked = await lock_workspace_for_update(db_session, ws.id)
        assert locked.id == ws.id

    @pytest.mark.asyncio
    async def test_missing_workspace_raises_notfound(self, db_session) -> None:
        with pytest.raises(NotFoundException):
            await lock_workspace_for_update(db_session, uuid4())

    @pytest.mark.asyncio
    async def test_soft_deleted_workspace_raises_notfound(self, db_session) -> None:
        ws = Workspace(
            id=uuid4(), name="WLockX", owner_user_id="o", plan_name="free", deleted_at=utcnow()
        )
        db_session.add(ws)
        await db_session.flush()

        with pytest.raises(NotFoundException):
            await lock_workspace_for_update(db_session, ws.id)

    @pytest.mark.asyncio
    async def test_populate_existing_refreshes_already_loaded_row(self, db_session) -> None:
        # Keystone of the #1102 erasure fix: when the row is ALREADY in the session
        # (e.g. loaded by account_erasure's unlocked SELECT), the locked re-SELECT
        # MUST refresh its attributes (populate_existing), not return stale cached
        # values — otherwise the under-lock owner re-check and the epoch
        # read-modify-write operate on a stale base.
        from sqlalchemy import update as sa_update

        ws = Workspace(id=uuid4(), name="WLockR", owner_user_id="orig", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        # Mutate the DB row out-of-band (stands in for a committed concurrent
        # transfer) WITHOUT touching the cached ORM instance — synchronize_session
        # =False keeps the in-session instance stale, exactly the identity-map
        # situation account_erasure hits.
        await db_session.execute(
            sa_update(Workspace)
            .where(Workspace.id == ws.id)
            .values(owner_user_id="moved")
            .execution_options(synchronize_session=False)
        )
        assert ws.owner_user_id == "orig"  # cached instance still stale

        locked = await lock_workspace_for_update(db_session, ws.id)
        assert locked is ws  # same identity-mapped instance
        assert locked.owner_user_id == "moved"  # ...but refreshed from the locked row


# ---------------------------------------------------------------------------
# update_member_context_access
# ---------------------------------------------------------------------------


class TestUpdateMemberContextAccess:
    @pytest.mark.asyncio
    async def test_set_allowed_contexts(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W11", owner_user_id="u19", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        db_session.add(
            WorkspaceMember(workspace_id=ws.id, user_id="u19", role=WorkspaceRole.MEMBER)
        )
        await db_session.flush()

        cid = uuid4()
        updated = await service.update_member_context_access(
            ws.id, "u19", allowed_context_ids=[cid]
        )
        assert updated.allowed_context_ids == [cid]


# ---------------------------------------------------------------------------
# get_workspace_stats
# ---------------------------------------------------------------------------


class TestGetWorkspaceStats:
    @pytest.mark.asyncio
    async def test_empty_workspace_stats(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W12", owner_user_id="u20", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u20", role=WorkspaceRole.OWNER))
        await db_session.flush()

        stats = await service.get_workspace_stats(ws.id)
        assert stats["context_count"] == 0
        assert stats["member_count"] == 1
        assert stats["total_memories"] == 0

    @pytest.mark.asyncio
    async def test_with_context_and_memories(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W13", owner_user_id="u21", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u21", role=WorkspaceRole.OWNER))
        await db_session.flush()

        ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name="ctx1",
            display_name="Ctx 1",
        )
        db_session.add(ctx)
        await db_session.flush()

        mem = Memory(
            id=uuid4(),
            user_id="u21",
            workspace_id=ws.id,
            context_id=ctx.id,
            summary="s",
            content="c",
            type="note",
            client="test",
            client_version="1.0",
        )
        db_session.add(mem)
        await db_session.flush()

        stats = await service.get_workspace_stats(ws.id)
        assert stats["context_count"] == 1
        assert stats["member_count"] == 1
        assert stats["total_memories"] == 1


# ---------------------------------------------------------------------------
# ensure_personal_workspace
# ---------------------------------------------------------------------------


class TestEnsurePersonalWorkspace:
    @pytest.mark.asyncio
    async def test_creates_when_no_workspace(self, db_session) -> None:
        service = WorkspaceService(db_session)
        user = User(
            user_id="u22",
            email="u22@example.com",
            name="User 22",
            role="user",
        )
        db_session.add(user)
        await db_session.flush()

        ws = await service.ensure_personal_workspace("u22", "u22@example.com")
        assert ws is not None
        assert ws.name == "Personal Workspace"

    @pytest.mark.asyncio
    async def test_skips_when_already_has_workspace(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W14", owner_user_id="u23", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        user = User(
            user_id="u23",
            email="u23@example.com",
            name="User 23",
            role="user",
            current_workspace_id=ws.id,
        )
        db_session.add(user)
        await db_session.flush()

        result = await service.ensure_personal_workspace("u23", "u23@example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_user_not_found(self, db_session) -> None:
        service = WorkspaceService(db_session)
        result = await service.ensure_personal_workspace("ghost", "ghost@example.com")
        assert result is None

    # ---- Issue #660 — three-branch decision ----

    @pytest.mark.asyncio
    async def test_branch_2_first_login_creates_workspace(self, db_session) -> None:
        """Branch (2): zero historical WorkspaceMember rows → create.

        First-login discriminator is `count(WorkspaceMember WHERE user_id=...) == 0`,
        NOT `last_login_at IS None`. `auth.roles.RoleManager.ensure_user` writes
        `last_login_at` whenever a User row is touched (creation or sync) — this
        runs BEFORE `ensure_personal_workspace` in every OAuth callback, so
        `last_login_at` is never None by the time we evaluate it.
        """
        from utils.datetime import utcnow

        service = WorkspaceService(db_session)
        user = User(
            user_id="u660-1",
            email="u660-1@example.com",
            name="First Login",
            role="user",
            # Simulate roles.py:326 having already written last_login_at.
            # The fix must NOT rely on this being None.
            last_login_at=utcnow(),
        )
        db_session.add(user)
        await db_session.flush()
        # No WorkspaceMember rows for this user — true first-ever login.

        ws = await service.ensure_personal_workspace("u660-1", "u660-1@example.com")
        assert ws is not None
        assert ws.name == "Personal Workspace"

    @pytest.mark.asyncio
    async def test_branch_3_returning_user_with_no_live_memberships_skipped(
        self, db_session
    ) -> None:
        """Branch (3): historical WorkspaceMember rows on soft-deleted workspaces
        but none on a live workspace → return None.

        This is the issue #660 core fix: existing user who deleted everything on
        purpose must not get a workspace silently recreated. The "has been part
        of the system before" signal is the orphan WorkspaceMember row that
        `delete_workspace` leaves behind when it soft-deletes the workspace.
        """
        from utils.datetime import utcnow

        service = WorkspaceService(db_session)
        # Soft-deleted workspace that the user used to belong to.
        dead_ws = Workspace(
            id=uuid4(),
            name="Old",
            owner_user_id="u660-2",
            plan_name="free",
            deleted_at=utcnow(),
        )
        db_session.add(dead_ws)
        await db_session.flush()
        # delete_workspace soft-deletes the workspace but leaves WorkspaceMember
        # rows intact — that is the "historical membership" signal.
        db_session.add(
            WorkspaceMember(workspace_id=dead_ws.id, user_id="u660-2", role=WorkspaceRole.OWNER)
        )

        user = User(
            user_id="u660-2",
            email="u660-2@example.com",
            name="Returning User",
            role="user",
            last_login_at=utcnow(),  # set by ensure_user, immaterial to the fix
        )
        db_session.add(user)
        await db_session.flush()

        result = await service.ensure_personal_workspace("u660-2", "u660-2@example.com")
        assert result is None
        # Crucially: user.current_workspace_id must remain None — no recreation.
        await db_session.refresh(user)
        assert user.current_workspace_id is None

    @pytest.mark.asyncio
    async def test_branch_1_picks_remaining_membership_and_skips_creation(self, db_session) -> None:
        """Branch (1): user is still a member of a non-deleted workspace.

        Sets current_workspace_id to that workspace and does NOT create a new
        Personal Workspace. Returns the selected workspace.
        """
        from utils.datetime import utcnow

        service = WorkspaceService(db_session)
        ws_remaining = Workspace(
            id=uuid4(), name="Remaining", owner_user_id="u660-3", plan_name="free"
        )
        db_session.add(ws_remaining)
        await db_session.flush()
        db_session.add(
            WorkspaceMember(
                workspace_id=ws_remaining.id, user_id="u660-3", role=WorkspaceRole.OWNER
            )
        )

        user = User(
            user_id="u660-3",
            email="u660-3@example.com",
            name="Has Memberships",
            role="user",
            last_login_at=utcnow(),
            # current_workspace_id intentionally None to model post-delete state
        )
        db_session.add(user)
        await db_session.flush()

        ws = await service.ensure_personal_workspace("u660-3", "u660-3@example.com")
        assert ws is not None
        assert ws.id == ws_remaining.id
        # No new "Personal Workspace" was created
        assert ws.name == "Remaining"
        await db_session.refresh(user)
        assert user.current_workspace_id == ws_remaining.id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "memberships, expected_workspace_name",
        [
            # Mixed roles: owner wins over member regardless of recency
            (
                [
                    ("owner_old", "owner", -86400 * 30),
                    ("member_new", "member", -3600),
                    ("owner_newer", "owner", -86400),
                ],
                "owner_newer",
            ),
            # Tied roles: most recent joined_at wins
            (
                [
                    ("m_old", "member", -86400 * 7),
                    ("m_new", "member", -3600),
                ],
                "m_new",
            ),
            # Admin beats member, even if member is more recent
            (
                [
                    ("admin_old", "admin", -86400 * 30),
                    ("member_recent", "member", -60),
                ],
                "admin_old",
            ),
            # Viewer is the lowest priority
            (
                [
                    ("viewer_recent", "viewer", -60),
                    ("member_old", "member", -86400 * 30),
                ],
                "member_old",
            ),
        ],
    )
    async def test_branch_1_role_preferring_selection_order(
        self,
        db_session,
        memberships: list[tuple[str, str, int]],
        expected_workspace_name: str,
    ) -> None:
        """AC #3: deterministic, role-preferring selection.

        Covers the #389 / PR #391 lesson — role-imprecise selection caused a
        cross-tenant leak. Order must be: owner > admin > member > viewer,
        tied by joined_at desc.
        """
        from datetime import timedelta

        from utils.datetime import utcnow

        service = WorkspaceService(db_session)
        now = utcnow()
        user_id = f"u660-order-{expected_workspace_name}"

        for ws_name, role, joined_at_offset_seconds in memberships:
            ws = Workspace(id=uuid4(), name=ws_name, owner_user_id=user_id, plan_name="free")
            db_session.add(ws)
            await db_session.flush()
            db_session.add(
                WorkspaceMember(
                    workspace_id=ws.id,
                    user_id=user_id,
                    role=role,
                    joined_at=now + timedelta(seconds=joined_at_offset_seconds),
                )
            )

        user = User(
            user_id=user_id,
            email=f"{user_id}@example.com",
            name="Selection Test",
            role="user",
            last_login_at=now,
        )
        db_session.add(user)
        await db_session.flush()

        selected = await service.ensure_personal_workspace(user_id, f"{user_id}@example.com")
        assert selected is not None
        assert selected.name == expected_workspace_name

    @pytest.mark.asyncio
    async def test_branch_1_skips_soft_deleted_workspaces(self, db_session) -> None:
        """Branch (1) must not select a soft-deleted workspace.

        Defense in depth — PermissionService.check_workspace_access already
        filters deleted workspaces (#276 / PR #329), but ensure_personal_workspace
        should not even set current_workspace_id to a deleted one.
        """
        from utils.datetime import utcnow

        service = WorkspaceService(db_session)
        live_ws = Workspace(id=uuid4(), name="Live", owner_user_id="u660-4", plan_name="free")
        dead_ws = Workspace(
            id=uuid4(),
            name="Dead",
            owner_user_id="u660-4",
            plan_name="free",
            deleted_at=utcnow(),
        )
        db_session.add_all([live_ws, dead_ws])
        await db_session.flush()
        db_session.add_all(
            [
                WorkspaceMember(
                    workspace_id=live_ws.id, user_id="u660-4", role=WorkspaceRole.MEMBER
                ),
                # Membership row on a soft-deleted workspace can exist — Workspace.deleted_at IS NULL filter is what excludes it.
                WorkspaceMember(
                    workspace_id=dead_ws.id, user_id="u660-4", role=WorkspaceRole.OWNER
                ),
            ]
        )

        user = User(
            user_id="u660-4",
            email="u660-4@example.com",
            name="Mixed",
            role="user",
            last_login_at=utcnow(),
        )
        db_session.add(user)
        await db_session.flush()

        ws = await service.ensure_personal_workspace("u660-4", "u660-4@example.com")
        assert ws is not None
        assert ws.id == live_ws.id  # NOT dead_ws even though dead_ws has owner role

    @pytest.mark.asyncio
    async def test_branch_1_deterministic_when_joined_at_all_null(self, db_session) -> None:
        """Selection stays deterministic when every membership ties on role
        AND `joined_at IS NULL`. WorkspaceMember.joined_at is nullable on the
        model — legacy or backfilled rows can lack it. The final UUID tiebreaker
        guarantees the same workspace is picked every time."""
        from utils.datetime import utcnow

        service = WorkspaceService(db_session)
        ws_a = Workspace(id=uuid4(), name="A", owner_user_id="u660-5", plan_name="free")
        ws_b = Workspace(id=uuid4(), name="B", owner_user_id="u660-5", plan_name="free")
        db_session.add_all([ws_a, ws_b])
        await db_session.flush()
        # Both memberships have role=member and joined_at=None — without the
        # UUID tiebreaker, sort order depends on DB row order which has no ORDER BY.
        db_session.add_all(
            [
                WorkspaceMember(
                    workspace_id=ws_a.id,
                    user_id="u660-5",
                    role=WorkspaceRole.MEMBER,
                    joined_at=None,
                ),
                WorkspaceMember(
                    workspace_id=ws_b.id,
                    user_id="u660-5",
                    role=WorkspaceRole.MEMBER,
                    joined_at=None,
                ),
            ]
        )

        user = User(
            user_id="u660-5",
            email="u660-5@example.com",
            name="Tied",
            role="user",
            last_login_at=utcnow(),
        )
        db_session.add(user)
        await db_session.flush()

        # Two invocations must pick the same workspace; reset current_workspace_id
        # between calls to ensure both runs traverse branch (1) afresh.
        first = await service.ensure_personal_workspace("u660-5", "u660-5@example.com")
        assert first is not None
        await db_session.refresh(user)
        user.current_workspace_id = None
        await db_session.commit()
        second = await service.ensure_personal_workspace("u660-5", "u660-5@example.com")
        assert second is not None
        assert first.id == second.id
