"""Read-only observation credentials for the observability API (#2679).

An observation credential is a bearer secret (``kagura_obs_`` + 43 url-safe
characters) that a self-hosted dashboard holds instead of an owner key. It is
honoured by exactly one dependency, used by exactly one router
(``api/routes/observability.py``); every other route authenticates against
``api_keys`` and therefore rejects it. The binding is one workspace, or one
resource of that workspace, and it always expires.

Only the SHA-256 verifier of the secret is stored. Rotation mints a new row
that keeps the binding and the remaining lifetime and points back at the old
one (``rotated_from_id``); revocation sets ``revoked_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from utils.public_id import PublicIdPrefix, public_id_column

#: The two bindings a credential can have. A deployment-wide scope needs a
#: system-admin issuing surface and is not offered.
OBSERVATION_SCOPES: tuple[str, ...] = ("workspace", "resource")

OBSERVATION_NAME_MAX_CHARS = 100


class ObservationCredential(Base):
    """One scoped, read-only, expiring observation credential.

    Attributes:
        id: Internal primary key.
        public_id: The id that leaves the server (``obsk_`` + 22 base62).
        key_hash: SHA-256 hex of the secret.
        key_prefix: The secret's first 16 characters, for display.
        name: Owner-chosen label.
        workspace_id: The bound workspace.
        resource_pk: The bound resource (``resources.id``) when ``scope`` is
            ``resource``; NULL for a workspace-wide credential.
        scope: ``workspace`` or ``resource``.
        created_by: The owner who issued it.
        created_at: Issue time (naive UTC).
        expires_at: Mandatory expiry (naive UTC).
        last_used_at: Last accepted use, written at most once a minute.
        revoked_at: Revocation time; set means refused.
        rotated_from_id: The credential this one replaced, if any.
    """

    __tablename__ = "observation_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = public_id_column(PublicIdPrefix.OBSERVATION_KEY)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(OBSERVATION_NAME_MAX_CHARS), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_pk: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=True,
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rotated_from_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("observation_credentials.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "scope IN ('workspace', 'resource')",
            name="ck_observation_credentials_scope",
        ),
        CheckConstraint(
            "(scope = 'resource') = (resource_pk IS NOT NULL)",
            name="ck_observation_credentials_scope_resource",
        ),
        Index("ix_observation_credentials_workspace_id", "workspace_id"),
        Index("ix_observation_credentials_expires_at", "expires_at"),
        Index("ix_observation_credentials_revoked_at", "revoked_at"),
    )

    def __repr__(self) -> str:
        return f"<ObservationCredential(public_id='{self.public_id}', scope='{self.scope}')>"
