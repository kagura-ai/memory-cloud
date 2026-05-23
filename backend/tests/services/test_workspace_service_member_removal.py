"""Integration tests for WorkspaceMember removal with comprehensive cleanup.

Issue #275 Critical: Verify that all related records are properly cleaned up
or transferred when WorkspaceMember is removed.

Tests:
- ContextMembers deletion
- Memories transfer to owner
- Contexts ownership transfer
- ResourceTokens ownership transfer
- WorkspaceInvitations deletion
- ExternalAPIKeys deletion
"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from models.auth import (
    Context,
    ContextMember,
    ExternalAPIKey,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.memory import Memory
from models.resource import ResourceToken
from auth.workspace_roles import ContextRole, WorkspaceRole
from services.workspace_service import WorkspaceService
from utils.exceptions import ValidationError


@pytest.mark.asyncio
async def test_remove_member_comprehensive_cleanup(db_session):
    """Test comprehensive cleanup when removing a member.

    Issue #275 Critical: Verify all related records are cleaned up or transferred.

    Scenario:
    1. Create workspace with owner
    2. Add member (admin role)
    3. Create resources owned by member:
       - 2 contexts
       - 2 context memberships
       - 3 memories
       - 1 resource token
       - 1 pending invitation
       - 1 external API key
    4. Remove member from workspace
    5. Verify:
       - ContextMembers deleted
       - Memories transferred to owner
       - Contexts ownership transferred
       - ResourceTokens ownership transferred
       - Invitations deleted
       - ExternalAPIKeys deleted
       - WorkspaceMember deleted
    """
    workspace_service = WorkspaceService(db_session)

    owner_id = f"owner_{uuid4().hex[:8]}"
    member_id = f"member_{uuid4().hex[:8]}"

    # 1. Create workspace with owner
    workspace = Workspace(
        id=uuid4(),
        name=f"test-workspace-{uuid4().hex[:8]}",
        owner_user_id=owner_id,
        plan_name="pro",
        memory_limit=100000,
        daily_api_limit=10000,
        weekly_api_limit=50000,
    )
    db_session.add(workspace)

    owner_member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=owner_id,
        role=WorkspaceRole.OWNER,
    )
    db_session.add(owner_member)
    await db_session.commit()

    # 2. Add member
    await workspace_service.add_member(workspace.id, member_id, "admin")

    # 3. Create resources owned by member

    # 3a. Contexts created by member
    member_resource_id = f"res-{uuid4().hex[:8]}"
    context_by_member = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"member-context-{uuid4().hex[:8]}",
        created_by=member_id,
        is_private=False,
        resource_id=member_resource_id,
    )
    context_by_owner = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"owner-context-{uuid4().hex[:8]}",
        created_by=owner_id,
        is_private=False,
    )
    db_session.add_all([context_by_member, context_by_owner])
    await db_session.commit()

    # 3b. Context memberships
    context_membership = ContextMember(
        context_id=context_by_owner.id,
        user_id=member_id,
        role=ContextRole.EDITOR,
        invited_by=owner_id,
    )
    db_session.add(context_membership)

    # 3c. Memories created by member
    for i in range(3):
        memory = Memory(
            id=uuid4(),
            workspace_id=workspace.id,
            context_id=context_by_member.id,
            user_id=member_id,
            summary=f"Test memory {i}",
            content=f"Content {i}",
            type="note",
            client="test",
        )
        db_session.add(memory)

    # 3d. Resource token created by member (resource_id matches context_by_member.resource_id
    # so cleanup_member_resource_tokens can find and transfer it via Context.resource_id).
    # Issue #390 Phase 2: ResourceToken requires resource_pk — create a
    # backing Resource entity row first so the before_insert invariant
    # listener passes.
    from models.resource import Resource

    member_resource = Resource(
        id=uuid4(),
        workspace_id=workspace.id,
        resource_id=member_resource_id,
        name="member-test-resource",
        created_by=member_id,
    )
    db_session.add(member_resource)
    await db_session.flush()

    resource_token = ResourceToken(
        resource_pk=member_resource.id,
        resource_id=member_resource_id,
        workspace_id=workspace.id,
        token_hash="test_hash_123",
        created_by=member_id,
        description="Test token",
    )
    db_session.add(resource_token)

    # 3e. Pending invitation sent by member (token must be >= 20 chars per constraint)
    invitation = WorkspaceInvitation(
        workspace_id=workspace.id,
        email="invitee@example.com",
        role=WorkspaceRole.MEMBER,
        invited_by=member_id,
        token=f"invite_{uuid4().hex[:16]}",
    )
    db_session.add(invitation)

    # 3f. External API key
    ext_key = ExternalAPIKey(
        key_name="openai_api_key",
        provider="openai",
        encrypted_value="encrypted_value_123",
        user_id=member_id,
        workspace_id=workspace.id,
    )
    db_session.add(ext_key)

    await db_session.commit()

    # Verify initial state
    assert (
        await db_session.scalar(
            select(func.count(ContextMember.id)).where(ContextMember.user_id == member_id)
        )
    ) == 1
    assert (
        await db_session.scalar(select(func.count(Memory.id)).where(Memory.user_id == member_id))
    ) == 3
    assert (
        await db_session.scalar(
            select(func.count(Context.id)).where(Context.created_by == member_id)
        )
    ) == 1
    assert (
        await db_session.scalar(
            select(func.count(ResourceToken.id)).where(ResourceToken.created_by == member_id)
        )
    ) == 1
    assert (
        await db_session.scalar(
            select(func.count(WorkspaceInvitation.id)).where(
                WorkspaceInvitation.invited_by == member_id
            )
        )
    ) == 1
    assert (
        await db_session.scalar(
            select(func.count(ExternalAPIKey.id)).where(ExternalAPIKey.user_id == member_id)
        )
    ) == 1

    # 4. Remove member from workspace
    await workspace_service.remove_member(workspace.id, member_id)

    # 5. Verify cleanup/transfers

    # ContextMembers deleted
    assert (
        await db_session.scalar(
            select(func.count(ContextMember.id)).where(ContextMember.user_id == member_id)
        )
    ) == 0

    # Memories transferred to owner
    member_memories = await db_session.scalar(
        select(func.count(Memory.id)).where(Memory.user_id == member_id)
    )
    owner_memories = await db_session.scalar(
        select(func.count(Memory.id)).where(Memory.user_id == owner_id)
    )
    assert member_memories == 0, "Member should have no memories"
    assert owner_memories >= 3, "Owner should have received transferred memories"

    # Contexts ownership transferred
    member_contexts = await db_session.scalar(
        select(func.count(Context.id)).where(Context.created_by == member_id)
    )
    owner_contexts = await db_session.scalar(
        select(func.count(Context.id)).where(Context.created_by == owner_id)
    )
    assert member_contexts == 0, "Member should have no contexts"
    assert owner_contexts >= 1, "Owner should have received transferred contexts"

    # ResourceTokens ownership transferred
    member_tokens = await db_session.scalar(
        select(func.count(ResourceToken.id)).where(ResourceToken.created_by == member_id)
    )
    owner_tokens = await db_session.scalar(
        select(func.count(ResourceToken.id)).where(ResourceToken.created_by == owner_id)
    )
    assert member_tokens == 0, "Member should have no resource tokens"
    assert owner_tokens >= 1, "Owner should have received transferred tokens"

    # Invitations deleted
    assert (
        await db_session.scalar(
            select(func.count(WorkspaceInvitation.id)).where(
                WorkspaceInvitation.invited_by == member_id
            )
        )
    ) == 0

    # ExternalAPIKeys deleted
    assert (
        await db_session.scalar(
            select(func.count(ExternalAPIKey.id)).where(
                ExternalAPIKey.user_id == member_id, ExternalAPIKey.workspace_id == workspace.id
            )
        )
    ) == 0

    # WorkspaceMember deleted
    result = await db_session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == member_id,
        )
    )
    assert result.scalar_one_or_none() is None, "Workspace member should be deleted"


@pytest.mark.asyncio
async def test_remove_member_with_no_context_memberships(db_session):
    """Test removing a member who has no context memberships.

    Edge case: Member was never added to any contexts.
    Should succeed without errors.
    """
    workspace_service = WorkspaceService(db_session)

    owner_id = f"owner_{uuid4().hex[:8]}"
    member_id = f"member_{uuid4().hex[:8]}"

    # Create workspace with owner
    workspace = Workspace(
        id=uuid4(),
        name=f"test-workspace-{uuid4().hex[:8]}",
        owner_user_id=owner_id,
        plan_name="free",
        memory_limit=10000,
        daily_api_limit=1000,
        weekly_api_limit=5000,
    )
    db_session.add(workspace)

    owner_member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=owner_id,
        role=WorkspaceRole.OWNER,
    )
    db_session.add(owner_member)
    await db_session.commit()

    # Add member (but don't add to any contexts)
    await workspace_service.add_member(
        workspace_id=workspace.id,
        user_id=member_id,
        role=WorkspaceRole.MEMBER,
    )

    # Remove member (should succeed)
    await workspace_service.remove_member(workspace.id, member_id)

    # Verify member is deleted
    result = await db_session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == member_id,
        )
    )
    member = result.scalar_one_or_none()
    assert member is None


@pytest.mark.asyncio
async def test_remove_member_does_not_affect_other_members(db_session):
    """Test that removing one member doesn't delete other members' context memberships.

    Scenario:
    1. Create workspace with owner + 2 members
    2. Create shared context
    3. Add both members to context
    4. Remove member1
    5. Verify: member1's ContextMember deleted
    6. Verify: member2's ContextMember still exists
    """
    workspace_service = WorkspaceService(db_session)

    owner_id = f"owner_{uuid4().hex[:8]}"
    member1_id = f"member1_{uuid4().hex[:8]}"
    member2_id = f"member2_{uuid4().hex[:8]}"

    # Create workspace with owner
    workspace = Workspace(
        id=uuid4(),
        name=f"multi-member-workspace-{uuid4().hex[:8]}",
        owner_user_id=owner_id,
        plan_name="pro",
        memory_limit=100000,
        daily_api_limit=10000,
        weekly_api_limit=50000,
    )
    db_session.add(workspace)

    owner_member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=owner_id,
        role=WorkspaceRole.OWNER,
    )
    db_session.add(owner_member)
    await db_session.commit()

    # Add both members
    await workspace_service.add_member(workspace.id, member1_id, "member")
    await workspace_service.add_member(workspace.id, member2_id, "member")

    # Create shared context
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name="shared-context",
        created_by=owner_id,
        is_private=False,
    )
    db_session.add(context)
    await db_session.commit()

    # Add both members to context
    context_member_1 = ContextMember(
        context_id=context.id,
        user_id=member1_id,
        role=ContextRole.EDITOR,
        invited_by=owner_id,
    )
    context_member_2 = ContextMember(
        context_id=context.id,
        user_id=member2_id,
        role=ContextRole.EDITOR,
        invited_by=owner_id,
    )
    db_session.add_all([context_member_1, context_member_2])
    await db_session.commit()

    # Remove member1
    await workspace_service.remove_member(workspace.id, member1_id)

    # Verify member1's ContextMember deleted
    result = await db_session.execute(
        select(ContextMember).where(ContextMember.user_id == member1_id)
    )
    member1_contexts = result.scalars().all()
    assert len(member1_contexts) == 0

    # Verify member2's ContextMember still exists
    result = await db_session.execute(
        select(ContextMember).where(ContextMember.user_id == member2_id)
    )
    member2_contexts = result.scalars().all()
    assert len(member2_contexts) == 1
    assert member2_contexts[0].context_id == context.id


@pytest.mark.asyncio
async def test_cannot_remove_workspace_owner(db_session):
    """Test that workspace owner cannot be removed.

    Should raise ValidationError.
    """
    workspace_service = WorkspaceService(db_session)

    owner_id = f"owner_{uuid4().hex[:8]}"

    # Create workspace with owner
    workspace = Workspace(
        id=uuid4(),
        name=f"test-workspace-{uuid4().hex[:8]}",
        owner_user_id=owner_id,
        plan_name="free",
        memory_limit=10000,
        daily_api_limit=1000,
        weekly_api_limit=5000,
    )
    db_session.add(workspace)

    owner_member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=owner_id,
        role=WorkspaceRole.OWNER,
    )
    db_session.add(owner_member)
    await db_session.commit()

    # Try to remove owner (should fail)
    with pytest.raises(ValidationError, match="Cannot remove workspace owner"):
        await workspace_service.remove_member(workspace.id, owner_id)

    # Verify owner still exists
    result = await db_session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == owner_id,
        )
    )
    owner_member_check = result.scalar_one_or_none()
    assert owner_member_check is not None
    assert owner_member_check.role == "owner"
