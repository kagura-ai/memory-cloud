"""Worker app identities used for multi-app external-source dispatch (#1315).

An app identity represents one platform application (for example one Slack
app), not one customer installation.  ``app_key`` is the stable, non-secret
selector shared with kagura-bridge.  Signing secrets remain server-custody
credentials: only Fernet ciphertext is persisted and plaintext is exposed
solely by the service-token-protected worker bootstrap endpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class WorkerAppIdentity(Base):
    """A platform app identity and its bounded signing-secret rotation state."""

    __tablename__ = "worker_app_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    app_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unconfigured", server_default="unconfigured"
    )
    active_signing_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_secret_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retiring_signing_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    retiring_secret_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retiring_valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    config_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("platform", "app_key", name="uq_worker_app_identities_platform_key"),
        CheckConstraint(
            "platform IN ('slack', 'discord', 'teams')",
            name="check_worker_app_identity_platform",
        ),
        CheckConstraint(
            "status IN ('unconfigured', 'active', 'disabled')",
            name="check_worker_app_identity_status",
        ),
        CheckConstraint(
            "app_key ~ '^[a-z0-9][a-z0-9_-]{0,63}$'",
            name="check_worker_app_identity_key",
        ),
    )

    @staticmethod
    def _encrypt_secret(secret: str) -> str:
        from utils.encryption import get_encryptor

        if not secret:
            raise ValueError("signing secret must not be empty")
        return get_encryptor().encrypt(secret)

    @staticmethod
    def _decrypt_secret(ciphertext: str | None) -> str | None:
        from utils.encryption import get_encryptor

        return get_encryptor().decrypt(ciphertext) if ciphertext else None

    def set_active_signing_secret(self, secret: str | None) -> None:
        """Encrypt a new active secret, or clear it without retaining plaintext."""
        self.active_signing_secret_encrypted = self._encrypt_secret(secret) if secret else None

    def get_active_signing_secret(self) -> str | None:
        """Decrypt the active secret for the internal worker bootstrap only."""
        return self._decrypt_secret(self.active_signing_secret_encrypted)

    def set_retiring_signing_secret(self, secret: str | None) -> None:
        """Encrypt the temporarily accepted retiring secret, or clear it."""
        self.retiring_signing_secret_encrypted = self._encrypt_secret(secret) if secret else None

    def get_retiring_signing_secret(self) -> str | None:
        """Decrypt the retiring secret for the internal worker bootstrap only."""
        return self._decrypt_secret(self.retiring_signing_secret_encrypted)
