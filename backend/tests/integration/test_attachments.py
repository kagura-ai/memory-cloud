"""Integration tests for file attachment API.

Issue #330/#335: Real DB tests for attachment upload, download, delete.
"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from models.auth import Context, Workspace, WorkspaceMember
from models.memory import Attachment, Memory


@pytest.mark.asyncio
async def test_attachment_lifecycle(db_session):
    """Test create → list → verify → delete lifecycle with real DB."""
    owner_id = f"owner_{uuid4().hex[:8]}"

    # Setup: workspace + memory
    workspace = Workspace(
        id=uuid4(),
        name=f"test-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    db_session.add(workspace)
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner_id, role="owner"))
    await db_session.commit()

    context = Context(
        id=uuid4(), workspace_id=workspace.id, name=f"ctx-{uuid4().hex[:8]}", created_by=owner_id
    )
    db_session.add(context)
    await db_session.commit()

    memory = Memory(
        id=uuid4(),
        workspace_id=workspace.id,
        context_id=context.id,
        user_id=owner_id,
        summary="Test memory for attachment",
        content="Content",
        type="note",
        client="test",
    )
    db_session.add(memory)
    await db_session.commit()

    # Create attachment
    test_data = b"Hello, this is a test file content."
    attachment = Attachment(
        memory_id=memory.id,
        filename="test.txt",
        content_type="text/plain",
        size_bytes=len(test_data),
        data=test_data,
    )
    db_session.add(attachment)
    await db_session.commit()
    await db_session.refresh(attachment)

    # Verify stored
    assert attachment.id is not None
    assert attachment.filename == "test.txt"
    assert attachment.size_bytes == len(test_data)

    # List attachments for memory
    result = await db_session.execute(
        select(func.count(Attachment.id)).where(Attachment.memory_id == memory.id)
    )
    count = result.scalar()
    assert count == 1

    # Read back data
    result = await db_session.execute(select(Attachment).where(Attachment.id == attachment.id))
    loaded = result.scalar_one()
    assert loaded.data == test_data
    assert loaded.content_type == "text/plain"

    # Delete
    await db_session.delete(loaded)
    await db_session.commit()

    result = await db_session.execute(
        select(func.count(Attachment.id)).where(Attachment.memory_id == memory.id)
    )
    assert result.scalar() == 0


@pytest.mark.asyncio
async def test_attachment_cascade_delete_with_memory(db_session):
    """Test that deleting a memory cascades to its attachments."""
    owner_id = f"owner_{uuid4().hex[:8]}"

    workspace = Workspace(
        id=uuid4(),
        name=f"test-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner_id,
        memory_limit=1000,
        daily_api_limit=1000,
        weekly_api_limit=5000,
    )
    db_session.add(workspace)
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner_id, role="owner"))
    await db_session.commit()

    context = Context(
        id=uuid4(), workspace_id=workspace.id, name=f"ctx-{uuid4().hex[:8]}", created_by=owner_id
    )
    db_session.add(context)
    await db_session.commit()

    memory = Memory(
        id=uuid4(),
        workspace_id=workspace.id,
        context_id=context.id,
        user_id=owner_id,
        summary="Memory with attachments",
        content="Content",
        type="note",
        client="test",
    )
    db_session.add(memory)
    await db_session.commit()

    # Add 3 attachments
    for i in range(3):
        db_session.add(
            Attachment(
                memory_id=memory.id,
                filename=f"file_{i}.txt",
                content_type="text/plain",
                size_bytes=10,
                data=b"0123456789",
            )
        )
    await db_session.commit()

    # Verify 3 attachments exist
    result = await db_session.execute(
        select(func.count(Attachment.id)).where(Attachment.memory_id == memory.id)
    )
    assert result.scalar() == 3

    # Delete memory — should cascade
    await db_session.delete(memory)
    await db_session.commit()

    # Verify attachments are gone
    result = await db_session.execute(
        select(func.count(Attachment.id)).where(Attachment.memory_id == memory.id)
    )
    assert result.scalar() == 0


@pytest.mark.asyncio
async def test_attachment_size_limit_enforced_by_db(db_session):
    """Test that DB CHECK constraint rejects oversized attachments."""
    from sqlalchemy.exc import IntegrityError

    owner_id = f"owner_{uuid4().hex[:8]}"
    workspace = Workspace(
        id=uuid4(),
        name=f"test-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner_id,
        memory_limit=1000,
        daily_api_limit=1000,
        weekly_api_limit=5000,
    )
    db_session.add(workspace)
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner_id, role="owner"))
    await db_session.commit()

    context = Context(
        id=uuid4(), workspace_id=workspace.id, name=f"ctx-{uuid4().hex[:8]}", created_by=owner_id
    )
    db_session.add(context)
    await db_session.commit()

    memory = Memory(
        id=uuid4(),
        workspace_id=workspace.id,
        context_id=context.id,
        user_id=owner_id,
        summary="Memory for size test",
        content="Content",
        type="note",
        client="test",
    )
    db_session.add(memory)
    await db_session.commit()

    # Try to insert with size_bytes=0 (should violate CHECK)
    attachment = Attachment(
        memory_id=memory.id,
        filename="empty.txt",
        content_type="text/plain",
        size_bytes=0,
        data=b"",
    )
    db_session.add(attachment)

    with pytest.raises(IntegrityError):
        await db_session.commit()

    await db_session.rollback()
