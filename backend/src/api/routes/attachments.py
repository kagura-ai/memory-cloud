"""Attachment API Routes.

Issue #330: File attachment support for memories.
Stored in PostgreSQL BYTEA with 5MB size limit.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser
from db.base import get_db
from models.auth import WorkspaceMember
from models.memory import Attachment, Memory
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/attachments", tags=["attachments"])

MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024  # 5MB
CHUNK_SIZE = 64 * 1024  # 64KB read chunks
ALLOWED_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
}


def _sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent header injection."""
    return filename.replace('"', "_").replace("\n", "_").replace("\r", "_").replace("\x00", "_")


class AttachmentInfo(BaseModel):
    """Attachment metadata (without binary data)."""

    id: str
    memory_id: str
    filename: str
    content_type: str
    size_bytes: int
    created_at: str


async def _verify_memory_access(memory_id: UUID, user: dict, db: AsyncSession) -> Memory:
    """Verify memory exists and user has workspace access."""
    result = await db.execute(select(Memory).where(Memory.id == memory_id))
    memory = result.scalar_one_or_none()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Verify user belongs to the memory's workspace
    if memory.workspace_id:
        user_id = user.get("user_id", "")
        member_result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == memory.workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
        if not member_result.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Access denied")

    return memory


async def _verify_attachment_access(
    attachment_id: UUID, user: dict, db: AsyncSession
) -> Attachment:
    """Verify attachment exists and user has workspace access via parent memory."""
    result = await db.execute(select(Attachment).where(Attachment.id == attachment_id))
    attachment = result.scalar_one_or_none()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    await _verify_memory_access(attachment.memory_id, user, db)
    return attachment


@router.post("/memories/{memory_id}", response_model=AttachmentInfo, status_code=201)
async def upload_attachment(
    memory_id: str,
    file: UploadFile,
    user: dict = Depends(APIKeyOrSessionUser),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file attachment to a memory.

    Limits: 5MB max, allowed MIME types only.
    """
    mem_uuid = UUID(memory_id)
    await _verify_memory_access(mem_uuid, user, db)

    # Validate content type
    content_type = file.content_type or "application/octet-stream"
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Content type '{content_type}' not allowed. Allowed: {', '.join(sorted(ALLOWED_CONTENT_TYPES))}",
        )

    # Read in chunks to avoid loading huge files into memory
    chunks = []
    total_size = 0
    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_ATTACHMENT_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File too large. Maximum: {MAX_ATTACHMENT_SIZE} bytes (5MB)",
            )
        chunks.append(chunk)

    if total_size == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    data = b"".join(chunks)

    attachment = Attachment(
        memory_id=mem_uuid,
        filename=_sanitize_filename(file.filename or "unnamed"),
        content_type=content_type,
        size_bytes=total_size,
        data=data,
    )
    db.add(attachment)
    await db.commit()
    await db.refresh(attachment)

    logger.info(
        "attachment_uploaded",
        attachment_id=str(attachment.id),
        memory_id=memory_id,
        filename=attachment.filename,
        size_bytes=attachment.size_bytes,
    )

    return AttachmentInfo(
        id=str(attachment.id),
        memory_id=str(attachment.memory_id),
        filename=attachment.filename,
        content_type=attachment.content_type,
        size_bytes=attachment.size_bytes,
        created_at=attachment.created_at.isoformat(),
    )


@router.get("/memories/{memory_id}", response_model=list[AttachmentInfo])
async def list_attachments(
    memory_id: str,
    user: dict = Depends(APIKeyOrSessionUser),
    db: AsyncSession = Depends(get_db),
):
    """List attachments for a memory (metadata only, no binary data)."""
    mem_uuid = UUID(memory_id)
    await _verify_memory_access(mem_uuid, user, db)

    result = await db.execute(
        select(Attachment).where(Attachment.memory_id == mem_uuid).order_by(Attachment.created_at)
    )
    attachments = result.scalars().all()

    return [
        AttachmentInfo(
            id=str(a.id),
            memory_id=str(a.memory_id),
            filename=a.filename,
            content_type=a.content_type,
            size_bytes=a.size_bytes,
            created_at=a.created_at.isoformat(),
        )
        for a in attachments
    ]


@router.get("/{attachment_id}")
async def download_attachment(
    attachment_id: str,
    user: dict = Depends(APIKeyOrSessionUser),
    db: AsyncSession = Depends(get_db),
):
    """Download an attachment file."""
    attachment = await _verify_attachment_access(UUID(attachment_id), user, db)

    return Response(
        content=attachment.data,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{_sanitize_filename(attachment.filename)}"',
            "Content-Length": str(attachment.size_bytes),
        },
    )


@router.delete("/{attachment_id}", status_code=204)
async def delete_attachment(
    attachment_id: str,
    user: dict = Depends(APIKeyOrSessionUser),
    db: AsyncSession = Depends(get_db),
):
    """Delete an attachment."""
    attachment = await _verify_attachment_access(UUID(attachment_id), user, db)

    await db.delete(attachment)
    await db.commit()

    logger.info("attachment_deleted", attachment_id=attachment_id)
