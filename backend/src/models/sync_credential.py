"""Operational service identities, public keys, replay receipts and token verifiers."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from utils.datetime import utcnow


class SyncCredentialClient(Base):
    """A non-human confidential client bound to one registered service."""

    __tablename__ = "sync_credential_clients"
    __table_args__ = (CheckConstraint("generation > 0", name="ck_sync_client_generation"),)

    client_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    service_id: Mapped[str] = mapped_column(
        ForeignKey("sync_services.service_id", ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now()
    )


class SyncSigningKey(Base):
    """Immutable public key identity; revocation cannot be undone by registration."""

    __tablename__ = "sync_signing_keys"
    __table_args__ = (
        UniqueConstraint("client_id", "kid", name="uq_sync_key_client_kid"),
        UniqueConstraint("client_id", "fingerprint", name="uq_sync_key_client_material"),
        CheckConstraint("length(fingerprint) = 64", name="ck_sync_key_fingerprint"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("sync_credential_clients.client_id", ondelete="CASCADE")
    )
    kid: Mapped[str] = mapped_column(String(128))
    public_jwk: Mapped[dict] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class SyncAssertionUse(Base):
    """Durable one-use receipt, independent of access-token issuance rollback."""

    __tablename__ = "sync_assertion_uses"
    __table_args__ = (CheckConstraint("length(jti_hash) = 64", name="ck_sync_assertion_hash"),)

    client_id: Mapped[str] = mapped_column(
        ForeignKey("sync_credential_clients.client_id", ondelete="CASCADE"), primary_key=True
    )
    jti_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now()
    )


class SyncAccessToken(Base):
    """Opaque token verifier and immutable admitted authority; never a human principal."""

    __tablename__ = "sync_access_tokens"
    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="ck_sync_access_hash"),
        CheckConstraint("expires_at > created_at", name="ck_sync_access_window"),
        CheckConstraint(
            "service_generation > 0 AND client_generation > 0 AND installation_generation > 0",
            name="ck_sync_access_generations",
        ),
        CheckConstraint("scope = 'resource.sync'", name="ck_sync_access_scope"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_installations.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    service_id: Mapped[str] = mapped_column(
        ForeignKey("sync_services.service_id", ondelete="CASCADE")
    )
    client_id: Mapped[str] = mapped_column(
        ForeignKey("sync_credential_clients.client_id", ondelete="CASCADE")
    )
    key_id: Mapped[UUID] = mapped_column(ForeignKey("sync_signing_keys.id", ondelete="CASCADE"))
    key_fingerprint: Mapped[str] = mapped_column(String(64))
    service_generation: Mapped[int] = mapped_column(Integer)
    client_generation: Mapped[int] = mapped_column(Integer)
    installation_generation: Mapped[int] = mapped_column(Integer)
    resource_pk: Mapped[UUID] = mapped_column(Uuid())
    context_id: Mapped[UUID] = mapped_column(Uuid())
    endpoint_id: Mapped[UUID] = mapped_column(Uuid())
    resource_id: Mapped[str] = mapped_column(String(255))
    operations: Mapped[list[str]] = mapped_column(JSONB)
    issuer: Mapped[str] = mapped_column(String(512))
    resource: Mapped[str] = mapped_column(String(640))
    scope: Mapped[str] = mapped_column(
        String(32), default="resource.sync", server_default="resource.sync"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
