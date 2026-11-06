"""Anonymous read-only public MCP endpoints (#2675).

A row publishes one resource context at ``/mcp/public/{public_id}``. It binds
the context and the resource identity, carries the public text the workspace
owner approved (never the context's internal summary or usage guide), and is
unpublished by setting ``revoked_at``. A re-publish is a new row with a new
``id`` and ``public_id``, so an old address never comes back.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from utils.public_id import PublicIdPrefix, public_id_column

PUBLIC_NAME_MAX_CHARS = 200
PUBLIC_DESCRIPTION_MAX_CHARS = 2_000
PUBLIC_USAGE_GUIDE_MAX_CHARS = 4_000


class PublicMcpEndpoint(Base):
    """One published, anonymous, read-only MCP endpoint over a resource context.

    Attributes:
        id: The endpoint identity; new on every publish.
        public_id: Opaque address segment (``pmcp_`` + 22 base62).
        workspace_id: Owning workspace.
        context_id: The bound resource context.
        resource_pk: The bound resource identity (``resources.id``).
        resource_id: Slug snapshot; must still equal the context's at serve time.
        public_name: Approved public name.
        public_description: Approved public description.
        public_usage_guide: Approved public usage guide.
        created_by: The owner who published it.
        created_at: Publish time (naive UTC).
        updated_at: Last approved-text edit.
        revoked_at: Unpublish time; set means not served.
        revoked_by: Who unpublished it.
    """

    __tablename__ = "public_mcp_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    public_id: Mapped[str] = public_id_column(PublicIdPrefix.PUBLIC_MCP)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contexts.id", ondelete="CASCADE"), nullable=False
    )
    resource_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    public_name: Mapped[str] = mapped_column(String(PUBLIC_NAME_MAX_CHARS), nullable=False)
    public_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_usage_guide: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # One live endpoint per context; revoked rows stay as the audit trail.
        Index(
            "ux_public_mcp_endpoints_context_active",
            "context_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
