"""Tests for services/workspace_service.py.

Covers CRUD, member management, and statistics.
Heavy dependencies (ContextService, Qdrant) are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import Memory
from services.workspace_service import WorkspaceService
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

        member = WorkspaceMember(workspace_id=ws.id, user_id="u2", role="owner")
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

        member = await service.add_member(ws.id, "u5", role="member")
        assert member.user_id == "u5"
        assert member.role == "member"

    @pytest.mark.asyncio
    async def test_add_duplicate_raises(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W4", owner_user_id="u6", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        await service.add_member(ws.id, "u7", role="member")
        with pytest.raises(ValidationError, match="already a member"):
            await service.add_member(ws.id, "u7", role="member")


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

        member = WorkspaceMember(workspace_id=ws.id, user_id="u9", role="admin")
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

        member = WorkspaceMember(workspace_id=ws.id, user_id="u11", role="member")
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

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u12", role="owner"))
        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u13", role="member"))
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

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u14", role="owner"))
        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u15", role="member"))
        await db_session.flush()

        updated = await service.update_member_role(ws.id, "u15", "admin")
        assert updated.role == "admin"

    @pytest.mark.asyncio
    async def test_promote_to_owner_blocked(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W9", owner_user_id="u16", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u16", role="owner"))
        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u17", role="member"))
        await db_session.flush()

        with pytest.raises(ValidationError, match="already has an owner"):
            await service.update_member_role(ws.id, "u17", "owner")

    @pytest.mark.asyncio
    async def test_demote_owner(self, db_session) -> None:
        service = WorkspaceService(db_session)
        ws = Workspace(id=uuid4(), name="W10", owner_user_id="u18", plan_name="free")
        db_session.add(ws)
        await db_session.flush()

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u18", role="owner"))
        await db_session.flush()

        updated = await service.update_member_role(ws.id, "u18", "admin")
        assert updated.role == "admin"


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

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u19", role="member"))
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

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u20", role="owner"))
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

        db_session.add(WorkspaceMember(workspace_id=ws.id, user_id="u21", role="owner"))
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
